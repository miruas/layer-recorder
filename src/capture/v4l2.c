#include "../../include/capture/v4l2.h"
#include "../../include/log.h"
#include "../../include/color_conversion.h"
#include "../../include/egl.h"
#include "../../include/utils.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <unistd.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/videodev2.h>
#include <linux/dma-buf.h>
#include <drm_fourcc.h>

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
#include <assert.h>

#define TJPF_RGB 0
#define TJPF_RGBA 7
#define TJFLAG_FASTDCT  2048

#define NUM_BUFFERS 4
#define NUM_PBOS 2

typedef void* tjhandle;
typedef tjhandle (*FUNC_tjInitDecompress)(void);
typedef int (*FUNC_tjDestroy)(tjhandle handle);
typedef int (*FUNC_tjDecompressHeader2)(tjhandle handle,
                                        unsigned char *jpegBuf, unsigned long jpegSize,
                                        int *width, int *height, int *jpegSubsamp);
typedef int (*FUNC_tjDecompress2)(tjhandle handle, const unsigned char *jpegBuf,
                                  unsigned long jpegSize, unsigned char *dstBuf,
                                  int width, int pitch, int height, int pixelFormat,
                                  int flags);
typedef char* (*FUNC_tjGetErrorStr2)(tjhandle handle);

typedef enum {
    V4L2_BUFFER_TYPE_DMABUF,
    V4L2_BUFFER_TYPE_MMAP
} v4l2_buffer_type;

typedef struct {
    bool yuyv;
    bool mjpeg;
} gsr_capture_v4l2_supported_pixfmts;

typedef struct {
    gsr_capture_v4l2_params params;
    vec2i capture_size;

    bool should_stop;
    bool stop_is_error;

    int fd;
    int dmabuf_fd[NUM_BUFFERS];
    EGLImage dma_image[NUM_BUFFERS];
    unsigned int texture_id[NUM_BUFFERS];
    bool buffer_held[NUM_BUFFERS];
    void *buffer_draw_fence[NUM_BUFFERS];
    unsigned int prev_texture_index;
    bool got_first_frame;

    void *dmabuf_map[NUM_BUFFERS];
    size_t dmabuf_size[NUM_BUFFERS];
    unsigned int pbos[NUM_PBOS];
    unsigned int pbo_index;
    
    v4l2_buffer_type buffer_type;

    void *libturbojpeg_lib;
    FUNC_tjInitDecompress tjInitDecompress;
    FUNC_tjDestroy tjDestroy;
    FUNC_tjDecompressHeader2 tjDecompressHeader2;
    FUNC_tjDecompress2 tjDecompress2;
    FUNC_tjGetErrorStr2 tjGetErrorStr2;
    tjhandle jpeg_decompressor;

    double capture_start_time;

    bool yuyv_conversion_fallback;
} gsr_capture_v4l2;

static int xioctl(int fd, unsigned long request, void *arg) {
    int r;

    do {
        r = ioctl(fd, request, arg);
    } while (-1 == r && EINTR == errno);

    return r;
}

static void gsr_capture_v4l2_stop(gsr_capture_v4l2 *self) {
    for(int i = 0; i < NUM_BUFFERS; ++i) {
        if(self->buffer_draw_fence[i]) {
            self->params.egl->glDeleteSync(self->buffer_draw_fence[i]);
            self->buffer_draw_fence[i] = NULL;
        }
        self->buffer_held[i] = false;
    }

    self->params.egl->glDeleteBuffers(NUM_PBOS, self->pbos);
    for(int i = 0; i < NUM_PBOS; ++i) {
        self->pbos[i] = 0;
    }

    self->params.egl->glDeleteTextures(NUM_BUFFERS, self->texture_id);
    for(int i = 0; i < NUM_BUFFERS; ++i) {
        self->texture_id[i] = 0;
    }

    for(int i = 0; i < NUM_BUFFERS; ++i) {
        if(self->dmabuf_map[i]) {
            munmap(self->dmabuf_map[i], self->dmabuf_size[i]);
            self->dmabuf_map[i] = NULL;
        }

        if(self->dma_image[i]) {
            self->params.egl->eglDestroyImage(self->params.egl->egl_display, self->dma_image[i]);
            self->dma_image[i] = NULL;
        }

        if(self->dmabuf_fd[i] > 0) {
            close(self->dmabuf_fd[i]);
            self->dmabuf_fd[i] = 0;
        }
    }

    if(self->fd > 0) {
        xioctl(self->fd, VIDIOC_STREAMOFF, &(enum v4l2_buf_type){V4L2_BUF_TYPE_VIDEO_CAPTURE});
        close(self->fd);
        self->fd = 0;
    }

    if(self->jpeg_decompressor) {
        self->tjDestroy(self->jpeg_decompressor);
        self->jpeg_decompressor = NULL;
    }

    if(self->libturbojpeg_lib) {
        dlclose(self->libturbojpeg_lib);
        self->libturbojpeg_lib = NULL;
    }
}

static void gsr_capture_v4l2_reset_cropping(gsr_capture_v4l2 *self) {
    struct v4l2_cropcap cropcap = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    };
    if(xioctl(self->fd, VIDIOC_CROPCAP, &cropcap) == 0) {
        struct v4l2_crop crop = {
            .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
            .c = cropcap.defrect /* reset to default */
        };

        if(xioctl(self->fd, VIDIOC_S_CROP, &crop) == -1) {
            switch (errno) {
            case EINVAL:
                /* Cropping not supported. */
                break;
            default:
                /* Errors ignored. */
                break;
            }
        }
    } else {
        /* Errors ignored. */
    }
}

static uint32_t gsr_pixfmt_to_v4l2_pixfmt(gsr_capture_v4l2_pixfmt pixfmt) {
    switch(pixfmt) {
        case GSR_CAPTURE_V4L2_PIXFMT_AUTO:
            assert(false);
            break;
        case GSR_CAPTURE_V4L2_PIXFMT_YUYV:
            return V4L2_PIX_FMT_YUYV;
        case GSR_CAPTURE_V4L2_PIXFMT_MJPEG:
            return V4L2_PIX_FMT_MJPEG;
    }
    assert(false);
    return V4L2_PIX_FMT_YUYV;
}

const char* gsr_capture_v4l2_pixfmt_to_string(gsr_capture_v4l2_pixfmt pixfmt) {
    switch(pixfmt) {
        case GSR_CAPTURE_V4L2_PIXFMT_AUTO:
            assert(false);
            break;
        case GSR_CAPTURE_V4L2_PIXFMT_YUYV:
            return "yuyv";
        case GSR_CAPTURE_V4L2_PIXFMT_MJPEG:
            return "mjpeg";
    }
    assert(false);
    return "";
}

static gsr_capture_v4l2_supported_pixfmts gsr_capture_v4l2_get_supported_pixfmts(int fd) {
    gsr_capture_v4l2_supported_pixfmts result = {0};

    struct v4l2_fmtdesc fmt = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE
    };
    while(xioctl(fd, VIDIOC_ENUM_FMT, &fmt) == 0) {
        //fprintf(stderr, "fmt: %d, desc: %s, flags: %d\n", fmt.pixelformat, fmt.description, fmt.flags);
        switch(fmt.pixelformat) {
            case V4L2_PIX_FMT_YUYV:
                result.yuyv = true;
                break;
            case V4L2_PIX_FMT_MJPEG:
                result.mjpeg = true;
                break;
        }
        ++fmt.index;
    }

    return result;
}

/* Returns the number of resolutions added */
static size_t gsr_capture_v4l2_get_supported_resolutions(int fd, gsr_capture_v4l2_pixfmt pixfmt, gsr_capture_v4l2_resolution *resolutions, size_t max_resolutions) {
    size_t resolution_index = 0;
    struct v4l2_frmsizeenum fmt = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .pixel_format = gsr_pixfmt_to_v4l2_pixfmt(pixfmt),
    };

    while(xioctl(fd, VIDIOC_ENUM_FRAMESIZES, &fmt) == 0) {
        if(fmt.type == V4L2_FRMSIZE_TYPE_DISCRETE && resolution_index < max_resolutions) {
            // Skip unsupported resolutions for now (those that eglCreateImage cant import because of pitch hardware limitation).
            // TODO: Find a fix for this.
            if(pixfmt == GSR_CAPTURE_V4L2_PIXFMT_YUYV && (fmt.discrete.width % 128 != 0)) {
                ++fmt.index;
                continue;
            }

            resolutions[resolution_index] = (gsr_capture_v4l2_resolution){
                .width = fmt.discrete.width,
                .height = fmt.discrete.height,
            };
            ++resolution_index;
        }
        ++fmt.index;
    }

    return resolution_index;
}

/* Returns the number of framerates added */
static size_t gsr_capture_v4l2_get_supported_framerates(int fd, gsr_capture_v4l2_pixfmt pixfmt, gsr_capture_v4l2_resolution resolution, gsr_capture_v4l2_framerate *framerates, size_t max_framerates) {
    size_t framerate_index = 0;
    struct v4l2_frmivalenum fmt = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .pixel_format = gsr_pixfmt_to_v4l2_pixfmt(pixfmt),
        .width = resolution.width,
        .height = resolution.height,
    };

    while(xioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, &fmt) == 0) {
        if(fmt.type == V4L2_FRMIVAL_TYPE_DISCRETE && fmt.discrete.denominator > 0 && fmt.discrete.numerator > 0 && framerate_index < max_framerates) {
            framerates[framerate_index] = (gsr_capture_v4l2_framerate){
                .denominator = fmt.discrete.denominator,
                .numerator = fmt.discrete.numerator,
            };
            ++framerate_index;
        }
        ++fmt.index;
    }

    return framerate_index;
}

/* Returns the number of setups added */
static size_t gsr_capture_v4l2_get_supported_setups(int fd, gsr_capture_v4l2_supported_setup *supported_setups, size_t max_supported_setups, bool has_libturbojpeg_lib) {
    const gsr_capture_v4l2_supported_pixfmts supported_pixfmts = gsr_capture_v4l2_get_supported_pixfmts(fd);

    size_t num_pixfmts = 0;
    gsr_capture_v4l2_pixfmt pixfmts[2];

    if(supported_pixfmts.yuyv)
        pixfmts[num_pixfmts++] = GSR_CAPTURE_V4L2_PIXFMT_YUYV;

    if(supported_pixfmts.mjpeg && has_libturbojpeg_lib)
        pixfmts[num_pixfmts++] = GSR_CAPTURE_V4L2_PIXFMT_MJPEG;

    gsr_capture_v4l2_resolution resolutions[32];
    gsr_capture_v4l2_framerate framerates[32];
    size_t supported_setup_index = 0;

    for(size_t pixfmt_index = 0; pixfmt_index < num_pixfmts; ++pixfmt_index) {
        const gsr_capture_v4l2_pixfmt pixfmt = pixfmts[pixfmt_index];
        const size_t num_resolutions = gsr_capture_v4l2_get_supported_resolutions(fd, pixfmt, resolutions, 32);

        for(size_t resolution_index = 0; resolution_index < num_resolutions; ++resolution_index) {
            const gsr_capture_v4l2_resolution resolution = resolutions[resolution_index];
            const size_t num_framerates = gsr_capture_v4l2_get_supported_framerates(fd, pixfmt, resolution, framerates, 32);

            for(size_t framerate_index = 0; framerate_index < num_framerates; ++framerate_index) {
                const gsr_capture_v4l2_framerate framerate = framerates[framerate_index];

                if(supported_setup_index < max_supported_setups) {
                    supported_setups[supported_setup_index] = (gsr_capture_v4l2_supported_setup){
                        .pixfmt = pixfmt,
                        .resolution = resolution,
                        .framerate = framerate,
                    };
                    ++supported_setup_index;
                }
            }
        }
    }

    return supported_setup_index;
}

uint32_t gsr_capture_v4l2_framerate_to_number(gsr_capture_v4l2_framerate framerate) {
    return (uint32_t)((double)framerate.denominator / (double)framerate.numerator);
}

// TODO: Select the resolution closest to |camera_resolution|, if it's not 0, 0
static bool gsr_capture_v4l2_get_best_matching_setup(
    const gsr_capture_v4l2_supported_setup *supported_setups,
    size_t num_supported_setups,
    gsr_capture_v4l2_pixfmt pixfmt,
    uint32_t camera_fps,
    gsr_capture_v4l2_resolution camera_resolution,
    gsr_capture_v4l2_supported_setup *best_supported_setup)
{
    memset(best_supported_setup, 0, sizeof(*best_supported_setup));

    int best_match_index = -1;
    uint64_t best_match_score = 0;

    for(size_t i = 0; i < num_supported_setups; ++i) {
        const gsr_capture_v4l2_supported_setup *setup = &supported_setups[i];
        if(pixfmt != GSR_CAPTURE_V4L2_PIXFMT_AUTO && pixfmt != setup->pixfmt)
            continue;

        uint64_t setup_resolution_width = (uint64_t)setup->resolution.width;
        uint64_t setup_resolution_height = (uint64_t)setup->resolution.height;
        uint64_t setup_framerate = gsr_capture_v4l2_framerate_to_number(setup->framerate);

        if(setup_resolution_width == camera_resolution.width && setup_resolution_height == camera_resolution.height) {
            setup_resolution_width = 50000;
            setup_resolution_height = 50000;
        }

        if(setup_framerate == camera_fps) {
            setup_framerate = 50000;
        }

        const uint64_t match_score = setup_resolution_width * setup_resolution_height * setup_framerate + (pixfmt == GSR_CAPTURE_V4L2_PIXFMT_YUYV ? 5 : 0);
        if(match_score > best_match_score) {
            best_match_score = match_score;
            best_match_index = i;
        }

        //fprintf(stderr, "supported setup[%d]: pixfmt: %d, size: %ux%u, fps: %u/%u\n", (int)i, setup->pixfmt, setup->resolution.width, setup->resolution.height, setup->framerate.denominator, setup->framerate.numerator);
    }

    if(best_match_index == -1)
        return false;

    //fprintf(stderr, "best match index: %d\n", best_match_index);
    *best_supported_setup = supported_setups[best_match_index];
    return true;
}

/* Seems like some cameras need this? */
static void gsr_capture_v4l2_update_params(int fd) {
    struct v4l2_streamparm streamparm = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
    };
    if(xioctl(fd, VIDIOC_G_PARM, &streamparm) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_set_framerate: VIDIOC_G_PARM failed, error: %s", strerror(errno));
        return;
    }

    if(xioctl(fd, VIDIOC_S_PARM, &streamparm) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_set_framerate: VIDIOC_S_PARM failed, error: %s", strerror(errno));
        return;
    }
}

typedef struct {
    bool covers_drawn_size;
    uint32_t fps_rank;
    uint64_t drawn_area;
    uint64_t camera_area;
    bool yuyv;
} gsr_capture_v4l2_setup_rank;

static gsr_capture_v4l2_setup_rank gsr_capture_v4l2_get_setup_rank(const gsr_capture_v4l2_supported_setup *setup, uint32_t camera_fps, vec2i drawn_size_max) {
    const vec2i camera_size = { setup->resolution.width, setup->resolution.height };
    const vec2i drawn_size = scale_keep_aspect_ratio(camera_size, drawn_size_max);
    const uint32_t fps = gsr_capture_v4l2_framerate_to_number(setup->framerate);
    return (gsr_capture_v4l2_setup_rank){
        .covers_drawn_size = camera_size.x >= drawn_size.x && camera_size.y >= drawn_size.y,
        .fps_rank = camera_fps > 0 && fps == camera_fps ? UINT32_MAX : fps,
        .drawn_area = (uint64_t)drawn_size.x * (uint64_t)drawn_size.y,
        .camera_area = (uint64_t)camera_size.x * (uint64_t)camera_size.y,
        .yuyv = setup->pixfmt == GSR_CAPTURE_V4L2_PIXFMT_YUYV,
    };
}

static bool gsr_capture_v4l2_setup_rank_is_better(const gsr_capture_v4l2_setup_rank *a, const gsr_capture_v4l2_setup_rank *b) {
    if(a->covers_drawn_size != b->covers_drawn_size)
        return a->covers_drawn_size;

    if(a->fps_rank != b->fps_rank)
        return a->fps_rank > b->fps_rank;

    if(a->covers_drawn_size) {
        if(a->drawn_area != b->drawn_area)
            return a->drawn_area > b->drawn_area;
        if(a->camera_area != b->camera_area)
            return a->camera_area < b->camera_area;
    } else if(a->camera_area != b->camera_area) {
        return a->camera_area > b->camera_area;
    }

    return a->yuyv && !b->yuyv;
}

static bool gsr_capture_v4l2_get_best_setup_for_drawn_size(
    const gsr_capture_v4l2_supported_setup *supported_setups,
    size_t num_supported_setups,
    gsr_capture_v4l2_pixfmt pixfmt,
    uint32_t camera_fps,
    vec2i drawn_size_max,
    gsr_capture_v4l2_supported_setup *best_supported_setup)
{
    int best_match_index = -1;
    gsr_capture_v4l2_setup_rank best_match_rank = {0};

    for(size_t i = 0; i < num_supported_setups; ++i) {
        const gsr_capture_v4l2_supported_setup *setup = &supported_setups[i];
        if(pixfmt != GSR_CAPTURE_V4L2_PIXFMT_AUTO && pixfmt != setup->pixfmt)
            continue;

        const gsr_capture_v4l2_setup_rank rank = gsr_capture_v4l2_get_setup_rank(setup, camera_fps, drawn_size_max);
        if(best_match_index == -1 || gsr_capture_v4l2_setup_rank_is_better(&rank, &best_match_rank)) {
            best_match_index = i;
            best_match_rank = rank;
        }
    }

    if(best_match_index == -1)
        return false;

    *best_supported_setup = supported_setups[best_match_index];
    return true;
}

static bool gsr_capture_v4l2_resolution_equals(gsr_capture_v4l2_resolution a, gsr_capture_v4l2_resolution b) {
    return a.width == b.width && a.height == b.height;
}

static bool gsr_capture_v4l2_resolution_is_smaller(gsr_capture_v4l2_resolution a, gsr_capture_v4l2_resolution b) {
    return a.width <= b.width && a.height <= b.height && !gsr_capture_v4l2_resolution_equals(a, b);
}

static bool gsr_capture_v4l2_get_best_setup(
    const gsr_capture_v4l2 *self,
    const gsr_capture_v4l2_supported_setup *supported_setups,
    size_t num_supported_setups,
    vec2i drawn_size_max,
    gsr_capture_v4l2_supported_setup *best_supported_setup)
{
    const gsr_capture_v4l2_resolution camera_resolution = self->params.camera_resolution;
    const bool drawn_size_known = drawn_size_max.x > 0 && drawn_size_max.y > 0;
    if(!drawn_size_known)
        return gsr_capture_v4l2_get_best_matching_setup(supported_setups, num_supported_setups, self->params.pixfmt, self->params.camera_fps, camera_resolution, best_supported_setup);

    if(!gsr_capture_v4l2_get_best_setup_for_drawn_size(supported_setups, num_supported_setups, self->params.pixfmt, self->params.camera_fps, drawn_size_max, best_supported_setup))
        return false;

    if(camera_resolution.width == 0 || camera_resolution.height == 0 || gsr_capture_v4l2_resolution_equals(camera_resolution, best_supported_setup->resolution))
        return true;

    if(!gsr_capture_v4l2_resolution_is_smaller(camera_resolution, best_supported_setup->resolution)) {
        gsr_log(GSR_LOG_LEVEL_WARNING, "gsr_capture_v4l2_create: camera_width and camera_height (%ux%u) are ignored for %s because they are larger than the resolution that is needed for the size that the camera is drawn at (%dx%d)",
            camera_resolution.width, camera_resolution.height, self->params.device_path, drawn_size_max.x, drawn_size_max.y);
        return true;
    }

    gsr_capture_v4l2_supported_setup camera_resolution_setup;
    if(!gsr_capture_v4l2_get_best_matching_setup(supported_setups, num_supported_setups, self->params.pixfmt, self->params.camera_fps, camera_resolution, &camera_resolution_setup)
        || !gsr_capture_v4l2_resolution_equals(camera_resolution_setup.resolution, camera_resolution))
    {
        gsr_log(GSR_LOG_LEVEL_WARNING, "gsr_capture_v4l2_create: %s doesn't support the resolution %ux%u (camera_width and camera_height)", self->params.device_path, camera_resolution.width, camera_resolution.height);
        return true;
    }

    *best_supported_setup = camera_resolution_setup;
    return true;
}

static void gsr_capture_v4l2_set_framerate(int fd, gsr_capture_v4l2_framerate framerate) {
    struct v4l2_streamparm streamparm = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
    };
    if(xioctl(fd, VIDIOC_G_PARM, &streamparm) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_set_framerate: VIDIOC_G_PARM failed, error: %s", strerror(errno));
        return;
    }

    streamparm.parm.capture.timeperframe.denominator = framerate.denominator;
    streamparm.parm.capture.timeperframe.numerator = framerate.numerator;
    if(xioctl(fd, VIDIOC_S_PARM, &streamparm) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_set_framerate: VIDIOC_S_PARM failed, error: %s", strerror(errno));
        return;
    }

    if(streamparm.parm.capture.timeperframe.denominator == 0 || streamparm.parm.capture.timeperframe.numerator == 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_set_framerate: VIDIOC_S_PARM failed, error: invalid framerate: %u/%u", framerate.denominator, framerate.numerator);;
        return;
    }
}

static bool gsr_capture_v4l2_validate_pixfmt(const gsr_capture_v4l2 *self, const gsr_capture_v4l2_supported_pixfmts supported_pixfmts) {
    switch(self->params.pixfmt) {
        case GSR_CAPTURE_V4L2_PIXFMT_AUTO: {
            if(!supported_pixfmts.yuyv && !supported_pixfmts.mjpeg) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s doesn't support yuyv nor mjpeg. GPU Screen Recorder supports only yuyv and mjpeg at the moment. Report this as an issue, see: https://git.dec05eba.com/?p=about", self->params.device_path);
                return false;
            }
            break;
        }
        case GSR_CAPTURE_V4L2_PIXFMT_YUYV: {
            if(!supported_pixfmts.yuyv) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s doesn't support yuyv. Try recording with pixfmt=mjpeg or pixfmt=auto instead", self->params.device_path);
                return false;
            }
            break;
        }
        case GSR_CAPTURE_V4L2_PIXFMT_MJPEG: {
            if(!supported_pixfmts.mjpeg) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s doesn't support mjpeg. Try recording with pixfmt=yuyv or pixfmt=auto instead", self->params.device_path);
                return false;
            }
            break;
        }
    }
    return true;
}

static bool gsr_capture_v4l2_create_pbos(gsr_capture_v4l2 *self, int width, int height) {
    self->pbo_index = 0;

    self->params.egl->glGenBuffers(NUM_PBOS, self->pbos);
    for(int i = 0; i < NUM_PBOS; ++i) {
        if(self->pbos[i] == 0) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create_pbos: failed to create pixel buffer objects");
            return false;
        }

        self->params.egl->glBindBuffer(GL_PIXEL_UNPACK_BUFFER, self->pbos[i]);
        self->params.egl->glBufferData(GL_PIXEL_UNPACK_BUFFER, width * height * 4, 0, GL_DYNAMIC_DRAW);
    }

    self->params.egl->glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
    return true;
}

static bool gsr_capture_v4l2_map_buffer(gsr_capture_v4l2 *self, const struct v4l2_format *fmt) {
    switch(self->params.pixfmt) {
        case GSR_CAPTURE_V4L2_PIXFMT_AUTO: {
            assert(false);
            return false;
        }
        case GSR_CAPTURE_V4L2_PIXFMT_YUYV: {
            self->params.egl->glGenTextures(NUM_BUFFERS, self->texture_id);

            for(int i = 0; i < NUM_BUFFERS; ++i) {
                self->dma_image[i] = self->params.egl->eglCreateImage(self->params.egl->egl_display, 0, EGL_LINUX_DMA_BUF_EXT, NULL, (intptr_t[]) {
                    EGL_WIDTH, fmt->fmt.pix.width,
                    EGL_HEIGHT, fmt->fmt.pix.height,
                    EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_YUYV,
                    EGL_DMA_BUF_PLANE0_FD_EXT, self->dmabuf_fd[i],
                    EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
                    EGL_DMA_BUF_PLANE0_PITCH_EXT, fmt->fmt.pix.bytesperline,
                    EGL_NONE
                });

                if(!self->dma_image[i]) {
                    self->yuyv_conversion_fallback = true;
                    self->dma_image[i] = self->params.egl->eglCreateImage(self->params.egl->egl_display, 0, EGL_LINUX_DMA_BUF_EXT, NULL, (intptr_t[]) {
                        EGL_WIDTH, fmt->fmt.pix.width,
                        EGL_HEIGHT, fmt->fmt.pix.height,
                        EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_RG88,
                        EGL_DMA_BUF_PLANE0_FD_EXT, self->dmabuf_fd[i],
                        EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
                        EGL_DMA_BUF_PLANE0_PITCH_EXT, fmt->fmt.pix.bytesperline,
                        EGL_NONE
                    });

                    if(!self->dma_image[i]) {
                        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_map_buffer: eglCreateImage failed, error: %d", self->params.egl->eglGetError());
                        return false;
                    }
                }

                self->params.egl->glBindTexture(GL_TEXTURE_EXTERNAL_OES, self->texture_id[i]);
                self->params.egl->glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, self->dma_image[i]);
                self->params.egl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                self->params.egl->glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                self->params.egl->glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);
                if(self->texture_id[i] == 0) {
                    gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_map_buffer: failed to create texture");
                    return false;
                }
            }

            self->buffer_type = V4L2_BUFFER_TYPE_DMABUF;
            break;
        }
        case GSR_CAPTURE_V4L2_PIXFMT_MJPEG: {
            for(int i = 0; i < NUM_BUFFERS; ++i) {
                self->dmabuf_size[i] = fmt->fmt.pix.sizeimage;
                self->dmabuf_map[i] = mmap(NULL, fmt->fmt.pix.sizeimage, PROT_READ, MAP_SHARED, self->dmabuf_fd[i], 0);
                if(self->dmabuf_map[i] == MAP_FAILED) {
                    gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_map_buffer: mmap failed, error: %s", strerror(errno));
                    return false;
                }
            }

            // GL_RGBA is intentionally used here instead of GL_RGB, because the performance is much better when using glTexSubImage2D (22% cpu usage compared to 38% cpu usage)
            self->texture_id[0] = gl_create_texture(self->params.egl, fmt->fmt.pix.width, fmt->fmt.pix.height, GL_RGBA8, GL_RGBA, GL_LINEAR);
            if(self->texture_id[0] == 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_map_buffer: failed to create texture");
                return false;
            }

            if(!gsr_capture_v4l2_create_pbos(self, fmt->fmt.pix.width, fmt->fmt.pix.height))
                return false;

            self->buffer_type = V4L2_BUFFER_TYPE_MMAP;
            break;
        }
    }
    return true;
}

static bool is_libturbojpeg_library_available(void) {
    void *libturbojpeg_lib = dlopen("libturbojpeg.so.0", RTLD_LAZY);
    const bool has_libturbojpeg_lib = libturbojpeg_lib != NULL;
    if(libturbojpeg_lib)
        dlclose(libturbojpeg_lib);
    return has_libturbojpeg_lib;
}

static int gsr_capture_v4l2_setup(gsr_capture_v4l2 *self, vec2i drawn_size_max) {
    self->fd = open(self->params.device_path, O_RDWR | O_NONBLOCK);
    if(self->fd < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: failed to open %s, error: %s", self->params.device_path, strerror(errno));
        return -1;
    }

    struct v4l2_capability cap = {0};
    if(xioctl(self->fd, VIDIOC_QUERYCAP, &cap) == -1) {
        if(EINVAL == errno) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s isn't a v4l2 device", self->params.device_path);
            return -1;
        } else {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: VIDIOC_QUERYCAP failed, error: %s", strerror(errno));
            return -1;
        }
    }

    if(!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s isn't a video capture device", self->params.device_path);
        return -1;
    }

    if(!(cap.capabilities & V4L2_CAP_STREAMING)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s doesn't support streaming i/o", self->params.device_path);
        return -1;
    }

    gsr_capture_v4l2_reset_cropping(self);

    const bool has_libturbojpeg_lib = is_libturbojpeg_library_available();
    if(!has_libturbojpeg_lib && self->params.pixfmt == GSR_CAPTURE_V4L2_PIXFMT_AUTO) {
        gsr_log(GSR_LOG_LEVEL_WARNING, "gsr_capture_v4l2_create: libturbojpeg.so.0 isn't available on the system, yuyv camera capture will be used");
        self->params.pixfmt = GSR_CAPTURE_V4L2_PIXFMT_YUYV;
    }

    const gsr_capture_v4l2_supported_pixfmts supported_pixfmts = gsr_capture_v4l2_get_supported_pixfmts(self->fd);
    if(!gsr_capture_v4l2_validate_pixfmt(self, supported_pixfmts))
        return -1;

    gsr_capture_v4l2_supported_setup supported_setups[128];
    const size_t num_supported_setups = gsr_capture_v4l2_get_supported_setups(self->fd, supported_setups, 128, has_libturbojpeg_lib);

    gsr_capture_v4l2_supported_setup best_supported_setup = {0};
    if(!gsr_capture_v4l2_get_best_setup(self, supported_setups, num_supported_setups, drawn_size_max, &best_supported_setup)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: %s doesn't report any frame resolutions and framerates", self->params.device_path);
        return -1;
    }

    gsr_log(GSR_LOG_LEVEL_INFO, "gsr_capture_v4l2_create: capturing %s at %ux%u@%dhz, pixfmt: %s", self->params.device_path,
        best_supported_setup.resolution.width,
        best_supported_setup.resolution.height,
        gsr_capture_v4l2_framerate_to_number(best_supported_setup.framerate),
        gsr_capture_v4l2_pixfmt_to_string(best_supported_setup.pixfmt));

    gsr_capture_v4l2_update_params(self->fd);
    self->params.pixfmt = best_supported_setup.pixfmt;

    if(self->params.pixfmt == GSR_CAPTURE_V4L2_PIXFMT_MJPEG) {
        dlerror(); /* clear */
        self->libturbojpeg_lib = dlopen("libturbojpeg.so.0", RTLD_LAZY);
        if(!self->libturbojpeg_lib) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: failed to load libturbojpeg.so.0 which is required for camera mjpeg capture, error: %s", dlerror());
            return -1;
        }

        self->tjInitDecompress = (FUNC_tjInitDecompress)dlsym(self->libturbojpeg_lib, "tjInitDecompress");
        self->tjDestroy = (FUNC_tjDestroy)dlsym(self->libturbojpeg_lib, "tjDestroy");
        self->tjDecompressHeader2 = (FUNC_tjDecompressHeader2)dlsym(self->libturbojpeg_lib, "tjDecompressHeader2");
        self->tjDecompress2 = (FUNC_tjDecompress2)dlsym(self->libturbojpeg_lib, "tjDecompress2");
        self->tjGetErrorStr2 = (FUNC_tjGetErrorStr2)dlsym(self->libturbojpeg_lib, "tjGetErrorStr2");

        if(!self->tjInitDecompress || !self->tjDestroy || !self->tjDecompressHeader2 || !self->tjDecompress2 || !self->tjGetErrorStr2) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: libturbojpeg.so.0 is missing functions. The libturbojpeg version installed on your system might be outdated");
            return -1;
        }

        self->jpeg_decompressor = self->tjInitDecompress();
        if(!self->jpeg_decompressor) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: failed to create jpeg decompressor");
            return -1;
        }
    }

    const uint32_t v4l2_pixfmt = gsr_pixfmt_to_v4l2_pixfmt(self->params.pixfmt);
    struct v4l2_format fmt = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .fmt.pix.pixelformat = v4l2_pixfmt,
        .fmt.pix.width = best_supported_setup.resolution.width,
        .fmt.pix.height = best_supported_setup.resolution.height,
    };
    if(xioctl(self->fd, VIDIOC_S_FMT, &fmt) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: VIDIOC_S_FMT failed, error: %s", strerror(errno));
        return -1;
    }

    if(fmt.fmt.pix.pixelformat != v4l2_pixfmt) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: pixel format isn't as requested (got pixel format: %u, requested: %u), error: %s", fmt.fmt.pix.pixelformat, v4l2_pixfmt, strerror(errno));
        return -1;
    }

    self->capture_size.x = fmt.fmt.pix.width;
    self->capture_size.y = fmt.fmt.pix.height;

    gsr_capture_v4l2_set_framerate(self->fd, best_supported_setup.framerate);

    struct v4l2_requestbuffers reqbuf = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .memory = V4L2_MEMORY_MMAP,
        .count = NUM_BUFFERS
    };
    if(xioctl(self->fd, VIDIOC_REQBUFS, &reqbuf) == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: VIDIOC_REQBUFS failed, error: %s", strerror(errno));
        return -1;
    }

    for(int i = 0; i < NUM_BUFFERS; ++i) {
        struct v4l2_exportbuffer expbuf = {
            .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
            .index = i,
            .flags = O_RDONLY
        };
        if(xioctl(self->fd, VIDIOC_EXPBUF, &expbuf) == -1) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: VIDIOC_EXPBUF failed, error: %s", strerror(errno));
            return -1;
        }
        self->dmabuf_fd[i] = expbuf.fd;
    }

    if(!gsr_capture_v4l2_map_buffer(self, &fmt))
        return -1;

    for(int i = 0; i < NUM_BUFFERS; ++i) {
        struct v4l2_buffer buf = {
            .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
            .index = i,
            .memory = V4L2_MEMORY_MMAP
        };
        xioctl(self->fd, VIDIOC_QBUF, &buf);
    }

    if(xioctl(self->fd, VIDIOC_STREAMON, &(enum v4l2_buf_type){V4L2_BUF_TYPE_VIDEO_CAPTURE})) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create: VIDIOC_STREAMON failed, error: %s", strerror(errno));
        return -1;
    }

    gsr_log(GSR_LOG_LEVEL_INFO, "gsr_capture_v4l2_create: waiting for camera %s to be ready", self->params.device_path);
    return 0;
}

static int gsr_capture_v4l2_start(gsr_capture *cap, gsr_capture_metadata *capture_metadata) {
    gsr_capture_v4l2 *self = cap->priv;

    const int result = gsr_capture_v4l2_setup(self, capture_metadata->recording_size);
    if(result != 0) {
        gsr_capture_v4l2_stop(self);
        return result;
    }

    if(self->params.output_resolution.x == 0 && self->params.output_resolution.y == 0) {
        capture_metadata->video_size = self->capture_size;
    } else {
        self->params.output_resolution = scale_keep_aspect_ratio(self->capture_size, self->params.output_resolution);
        capture_metadata->video_size = self->params.output_resolution;
    }

    self->capture_start_time = clock_get_monotonic_seconds();
    return 0;
}

static void gsr_capture_v4l2_queue_buffer(gsr_capture_v4l2 *self, unsigned int buffer_index) {
    struct v4l2_buffer buf = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .memory = V4L2_MEMORY_MMAP,
        .index = buffer_index
    };
    xioctl(self->fd, VIDIOC_QBUF, &buf);
}

static void gsr_capture_v4l2_queue_drawn_buffers(gsr_capture_v4l2 *self, unsigned int buffer_index_to_keep) {
    for(unsigned int i = 0; i < NUM_BUFFERS; ++i) {
        if(!self->buffer_held[i] || i == buffer_index_to_keep)
            continue;

        if(self->buffer_draw_fence[i]) {
            const unsigned int status = self->params.egl->glClientWaitSync(self->buffer_draw_fence[i], 0, 0);
            if(status != GL_ALREADY_SIGNALED && status != GL_CONDITION_SATISFIED)
                continue;

            self->params.egl->glDeleteSync(self->buffer_draw_fence[i]);
            self->buffer_draw_fence[i] = NULL;
        }

        self->buffer_held[i] = false;
        gsr_capture_v4l2_queue_buffer(self, i);
    }
}

static void gsr_capture_v4l2_tick(gsr_capture *cap) {
    gsr_capture_v4l2 *self = cap->priv;
    if(self->buffer_type == V4L2_BUFFER_TYPE_DMABUF)
        gsr_capture_v4l2_queue_drawn_buffers(self, self->prev_texture_index);

    if(!self->got_first_frame && !self->should_stop) {
        const double timeout_sec = 5.0;
        if(clock_get_monotonic_seconds() - self->capture_start_time >= timeout_sec) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_capture: didn't receive camera data in %f seconds", timeout_sec);
            self->should_stop = true;
            self->stop_is_error = true;
        }
    }
}

static void gsr_capture_v4l2_decode_jpeg_to_texture(gsr_capture_v4l2 *self, const struct v4l2_buffer *buf) {
    int jpeg_subsamp = 0;
    int jpeg_width = 0;
    int jpeg_height = 0;
    if(self->tjDecompressHeader2(self->jpeg_decompressor, self->dmabuf_map[buf->index], buf->bytesused, &jpeg_width, &jpeg_height, &jpeg_subsamp) != 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_capture: failed to decompress camera jpeg header data, error: %s", self->tjGetErrorStr2(self->jpeg_decompressor));
        return;
    }

    if(jpeg_width != self->capture_size.x || jpeg_height != self->capture_size.y) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_capture: got jpeg data of incorrect dimensions. Expected %dx%d, got %dx%d", self->capture_size.x, self->capture_size.y, jpeg_width, jpeg_height);
        return;
    }

    self->params.egl->glBindTexture(GL_TEXTURE_2D, self->texture_id[0]);

    self->pbo_index = (self->pbo_index + 1) % NUM_PBOS;
    const unsigned int next_pbo_index = (self->pbo_index + 1) % NUM_PBOS;
    
    self->params.egl->glBindBuffer(GL_PIXEL_UNPACK_BUFFER, self->pbos[self->pbo_index]);
    self->params.egl->glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self->capture_size.x, self->capture_size.y, GL_RGBA, GL_UNSIGNED_BYTE, NULL);

    self->params.egl->glBindBuffer(GL_PIXEL_UNPACK_BUFFER, self->pbos[next_pbo_index]);
    self->params.egl->glBufferData(GL_PIXEL_UNPACK_BUFFER, self->capture_size.x * self->capture_size.y * 4, 0, GL_DYNAMIC_DRAW);

    void *mapped_buffer = self->params.egl->glMapBufferRange(GL_PIXEL_UNPACK_BUFFER, 0, self->capture_size.x * self->capture_size.y * 4, GL_MAP_WRITE_BIT);
    if(mapped_buffer) {
        if(self->tjDecompress2(self->jpeg_decompressor, self->dmabuf_map[buf->index], buf->bytesused, mapped_buffer, jpeg_width, 0, jpeg_height, TJPF_RGBA, TJFLAG_FASTDCT) != 0)
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_capture: failed to decompress camera jpeg data, error: %s", self->tjGetErrorStr2(self->jpeg_decompressor));
        self->params.egl->glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
    }

    self->params.egl->glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
    self->params.egl->glBindTexture(GL_TEXTURE_2D, 0);
}

static int gsr_capture_v4l2_capture(gsr_capture *cap, gsr_capture_metadata *capture_metadata, gsr_color_conversion *color_conversion) {
    (void)color_conversion;
    gsr_capture_v4l2 *self = cap->priv;

    struct v4l2_buffer buf = {
        .type = V4L2_BUF_TYPE_VIDEO_CAPTURE,
        .memory = V4L2_MEMORY_MMAP
    };

    const bool dequeued = xioctl(self->fd, VIDIOC_DQBUF, &buf) == 0;
    unsigned int texture_index = buf.index;

    if(dequeued && buf.bytesused > 0 && !(buf.flags & V4L2_BUF_FLAG_ERROR)) {
        if(!self->got_first_frame)
            gsr_log(GSR_LOG_LEVEL_INFO, "gsr_capture_v4l2_capture: camera %s is now ready", self->params.device_path);
        self->got_first_frame = true;

        switch(self->buffer_type) {
            case V4L2_BUFFER_TYPE_DMABUF: {
                //self->params.egl->glBindTexture(GL_TEXTURE_EXTERNAL_OES, self->texture_id);
                //self->params.egl->glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, self->dma_image[buf.index]);
                //self->params.egl->glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);
                break;
            }
            case V4L2_BUFFER_TYPE_MMAP: {
                //xioctl(self->dmabuf_fd[buf.index], DMA_BUF_IOCTL_SYNC, &(struct dma_buf_sync){ .flags = DMA_BUF_SYNC_START });
                gsr_capture_v4l2_decode_jpeg_to_texture(self, &buf);
                //xioctl(self->dmabuf_fd[buf.index], DMA_BUF_IOCTL_SYNC, &(struct dma_buf_sync){ .flags = DMA_BUF_SYNC_END });
                break;
            }
        }

        self->prev_texture_index = buf.index;
    } else {
        texture_index = self->prev_texture_index;
    }

    if(dequeued) {
        if(self->buffer_type == V4L2_BUFFER_TYPE_DMABUF && texture_index == buf.index)
            self->buffer_held[buf.index] = true;
        else
            gsr_capture_v4l2_queue_buffer(self, buf.index);
    }

    if(self->buffer_type == V4L2_BUFFER_TYPE_DMABUF)
        gsr_capture_v4l2_queue_drawn_buffers(self, texture_index);

    const vec2i output_size = scale_keep_aspect_ratio(self->capture_size, capture_metadata->recording_size);
    const vec2i target_pos = gsr_capture_get_target_position(output_size, capture_metadata);

    self->params.egl->glFlush();
    // TODO: Use the minimal barrier required
    self->params.egl->glMemoryBarrier(GL_ALL_BARRIER_BITS);

    if(self->buffer_type == V4L2_BUFFER_TYPE_DMABUF) {
        gsr_color_conversion_draw(color_conversion, self->texture_id[texture_index],
            target_pos, output_size,
            (vec2i){0, 0}, self->capture_size, self->capture_size,
            GSR_ROT_0, capture_metadata->flip, self->yuyv_conversion_fallback ? GSR_SOURCE_COLOR_YUYV : GSR_SOURCE_COLOR_RGB, true);

        if(self->buffer_held[texture_index]) {
            if(self->buffer_draw_fence[texture_index])
                self->params.egl->glDeleteSync(self->buffer_draw_fence[texture_index]);
            self->buffer_draw_fence[texture_index] = self->params.egl->glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        }
    } else {
        gsr_color_conversion_draw(color_conversion, self->texture_id[0],
            target_pos, output_size,
            (vec2i){0, 0}, self->capture_size, self->capture_size,
            GSR_ROT_0, capture_metadata->flip, GSR_SOURCE_COLOR_RGB, false);
    }

    return self->got_first_frame ? 0 : -1;
}

static bool gsr_capture_v4l2_uses_external_image(gsr_capture *cap) {
    (void)cap;
    return true;
}

static bool gsr_capture_v4l2_should_stop(gsr_capture *cap, bool *err) {
    gsr_capture_v4l2 *self = cap->priv;
    if(err)
        *err = self->stop_is_error;
    return self->should_stop;
}

static bool gsr_capture_v4l2_is_damaged(gsr_capture *cap) {
    gsr_capture_v4l2 *self = cap->priv;
    struct pollfd poll_data = {
        .fd = self->fd,
        .events = POLLIN,
        .revents = 0
    };
    return poll(&poll_data, 1, 0) > 0 && (poll_data.revents & POLLIN);
}

static void gsr_capture_v4l2_clear_damage(gsr_capture *cap) {
    gsr_capture_v4l2 *self = cap->priv;
    (void)self;
}

static void gsr_capture_v4l2_destroy(gsr_capture *cap) {
    gsr_capture_v4l2 *self = cap->priv;
    if(cap->priv) {
        gsr_capture_v4l2_stop(self);
        free(cap->priv);
        cap->priv = NULL;
    }
    free(cap);
}

gsr_capture* gsr_capture_v4l2_create(const gsr_capture_v4l2_params *params) {
    if(!params) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_v4l2_create params is NULL");
        return NULL;
    }

    gsr_capture *cap = calloc(1, sizeof(gsr_capture));
    if(!cap)
        return NULL;

    gsr_capture_v4l2 *cap_camera = calloc(1, sizeof(gsr_capture_v4l2));
    if(!cap_camera) {
        free(cap);
        return NULL;
    }

    cap_camera->params = *params;

    *cap = (gsr_capture) {
        .start = gsr_capture_v4l2_start,
        .tick = gsr_capture_v4l2_tick,
        .should_stop = gsr_capture_v4l2_should_stop,
        .capture = gsr_capture_v4l2_capture,
        .uses_external_image = gsr_capture_v4l2_uses_external_image,
        .is_damaged = gsr_capture_v4l2_is_damaged,
        .clear_damage = gsr_capture_v4l2_clear_damage,
        .destroy = gsr_capture_v4l2_destroy,
        .priv = cap_camera
    };

    return cap;
}

static int gsr_capture_v4l2_supported_setup_compare(const void *a, const void *b) {
    const gsr_capture_v4l2_supported_setup *setup_a = a;
    const gsr_capture_v4l2_supported_setup *setup_b = b;

    const uint64_t resolution_a = (uint64_t)setup_a->resolution.width * (uint64_t)setup_a->resolution.height;
    const uint64_t resolution_b = (uint64_t)setup_b->resolution.width * (uint64_t)setup_b->resolution.height;
    if(resolution_a != resolution_b)
        return resolution_a < resolution_b ? 1 : -1;

    const uint32_t framerate_a = gsr_capture_v4l2_framerate_to_number(setup_a->framerate);
    const uint32_t framerate_b = gsr_capture_v4l2_framerate_to_number(setup_b->framerate);
    if(framerate_a != framerate_b)
        return framerate_a < framerate_b ? 1 : -1;

    return 0;
}

void gsr_capture_v4l2_list_devices(v4l2_devices_query_callback callback, void *userdata) {
    const bool has_libturbojpeg_lib = is_libturbojpeg_library_available();
    char v4l2_device_path[128];
    gsr_capture_v4l2_supported_setup supported_setups[128];

    for(int i = 0; i < 8; ++i) {
        snprintf(v4l2_device_path, sizeof(v4l2_device_path), "/dev/video%d", i);

        const int fd = open(v4l2_device_path, O_RDWR | O_NONBLOCK);
        if(fd < 0)
            continue;

        struct v4l2_capability cap = {0};
        if(xioctl(fd, VIDIOC_QUERYCAP, &cap) == -1)
            goto next;

        if(!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE))
            goto next;

        if(!(cap.capabilities & V4L2_CAP_STREAMING))
            goto next;

        struct v4l2_format fmt = {
            .type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        };
        if(xioctl(fd, VIDIOC_G_FMT, &fmt) == -1)
            goto next;

        const size_t num_supported_setups = gsr_capture_v4l2_get_supported_setups(fd, supported_setups, 128, has_libturbojpeg_lib);
        if(num_supported_setups == 0)
            continue;

        qsort(supported_setups, num_supported_setups, sizeof(gsr_capture_v4l2_supported_setup), gsr_capture_v4l2_supported_setup_compare);

        for(size_t j = 0; j < num_supported_setups; ++j) {
            const gsr_capture_v4l2_supported_setup *setup = &supported_setups[j];
            callback(v4l2_device_path, setup, userdata);
        }

        next:
        close(fd);
    }
}
