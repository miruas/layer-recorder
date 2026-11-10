#include "../../../include/encoder/video/vaapi.h"
#include "../../../include/log.h"
#include "../../../include/utils.h"
#include "../../../include/egl.h"
#include "../../../include/ffmpeg_utils.h"

#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext_vaapi.h>
#include <libavutil/intreadwrite.h>

#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>

#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>

typedef struct {
    uint32_t drm_format;
    int size_divisor;
} gsr_video_encoder_vaapi_layer_format;

#define MAX_SURFACES 32

typedef struct {
    VASurfaceID surface_id;
    unsigned int textures[2];
    uint64_t clear_generation;
    uint64_t last_used;
} gsr_vaapi_surface;

typedef struct {
    gsr_video_encoder_vaapi_params params;

    gsr_vaapi_surface surfaces[MAX_SURFACES];
    int num_surfaces;
    uint64_t num_surface_lookups;
    vec2i texture_sizes[2];
    int num_textures;

    AVBufferRef *device_ctx;
    VADisplay va_dpy;
} gsr_video_encoder_vaapi;

static enum AVPixelFormat gsr_video_encoder_vaapi_get_sw_format(const gsr_video_encoder_vaapi_params *params) {
    const bool ten_bit = params->color_depth == GSR_COLOR_DEPTH_10_BITS;
    if(params->yuv444)
        return ten_bit ? AV_PIX_FMT_XV30LE : AV_PIX_FMT_VUYX;
    return ten_bit ? AV_PIX_FMT_P010LE : AV_PIX_FMT_NV12;
}

static gsr_destination_color gsr_video_encoder_vaapi_get_destination_color(const gsr_video_encoder_vaapi_params *params) {
    const bool ten_bit = params->color_depth == GSR_COLOR_DEPTH_10_BITS;
    if(params->yuv444)
        return ten_bit ? GSR_DESTINATION_COLOR_XV30 : GSR_DESTINATION_COLOR_VUYX;
    return ten_bit ? GSR_DESTINATION_COLOR_P010 : GSR_DESTINATION_COLOR_NV12;
}

static bool gsr_video_encoder_vaapi_setup_context(gsr_video_encoder_vaapi *self, AVCodecContext *video_codec_context) {
    char render_path[128];
    if(!gsr_card_path_get_render_path(self->params.egl->card_path, render_path)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_vaapi_setup_context: failed to get /dev/dri/renderDXXX file from %s", self->params.egl->card_path);
        return false;
    }

    if(av_hwdevice_ctx_create(&self->device_ctx, AV_HWDEVICE_TYPE_VAAPI, render_path, NULL, 0) < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_vaapi_setup_context: failed to create hardware device context");
        return false;
    }

    AVBufferRef *frame_context = av_hwframe_ctx_alloc(self->device_ctx);
    if(!frame_context) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_vaapi_setup_context: failed to create hwframe context");
        av_buffer_unref(&self->device_ctx);
        return false;
    }

    AVHWFramesContext *hw_frame_context = (AVHWFramesContext*)frame_context->data;
    hw_frame_context->width = video_codec_context->width;
    hw_frame_context->height = video_codec_context->height;
    hw_frame_context->sw_format = gsr_video_encoder_vaapi_get_sw_format(&self->params);
    hw_frame_context->format = video_codec_context->pix_fmt;
    hw_frame_context->device_ctx = (AVHWDeviceContext*)self->device_ctx->data;

    //hw_frame_context->initial_pool_size = 20;

    AVVAAPIDeviceContext *vactx = ((AVHWDeviceContext*)self->device_ctx->data)->hwctx;
    self->va_dpy = vactx->display;

    if (av_hwframe_ctx_init(frame_context) < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_vaapi_setup_context: failed to initialize hardware frame context (note: ffmpeg version needs to be > 4.0)");
        av_buffer_unref(&self->device_ctx);
        //av_buffer_unref(&frame_context);
        return false;
    }

    video_codec_context->hw_frames_ctx = av_buffer_ref(frame_context);
    av_buffer_unref(&frame_context);
    return true;
}

static uint32_t fourcc(uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
    return (d << 24) | (c << 16) | (b << 8) | a;
}

static int get_layer_formats(uint32_t va_fourcc, gsr_video_encoder_vaapi_layer_format *layer_formats) {
    switch(va_fourcc) {
        case VA_FOURCC_NV12:
            layer_formats[0] = (gsr_video_encoder_vaapi_layer_format){ fourcc('R', '8', ' ', ' '), 1 };
            layer_formats[1] = (gsr_video_encoder_vaapi_layer_format){ fourcc('G', 'R', '8', '8'), 2 };
            return 2;
        case VA_FOURCC_P010:
            layer_formats[0] = (gsr_video_encoder_vaapi_layer_format){ fourcc('R', '1', '6', ' '), 1 };
            layer_formats[1] = (gsr_video_encoder_vaapi_layer_format){ fourcc('G', 'R', '3', '2'), 2 };
            return 2;
#ifdef VA_FOURCC_XYUV
        case VA_FOURCC_XYUV:
            layer_formats[0] = (gsr_video_encoder_vaapi_layer_format){ fourcc('A', 'B', '2', '4'), 1 };
            return 1;
#endif
#ifdef VA_FOURCC_Y410
        case VA_FOURCC_Y410:
            layer_formats[0] = (gsr_video_encoder_vaapi_layer_format){ fourcc('A', 'B', '3', '0'), 1 };
            return 1;
#endif
    }
    return 0;
}

static void close_prime_fds(VADRMPRIMESurfaceDescriptor *prime) {
    for(uint32_t i = 0; i < prime->num_objects; ++i) {
        if(prime->objects[i].fd > 0)
            close(prime->objects[i].fd);
    }
}

static bool import_prime_layer(gsr_video_encoder_vaapi *self, const VADRMPRIMESurfaceDescriptor *prime, int layer, const gsr_video_encoder_vaapi_layer_format *layer_format, unsigned int texture) {
    int fds[4];
    uint32_t offsets[4];
    uint32_t pitches[4];
    uint64_t modifiers[4];
    for(uint32_t j = 0; j < prime->layers[layer].num_planes; ++j) {
        fds[j] = prime->objects[prime->layers[layer].object_index[j]].fd;
        offsets[j] = prime->layers[layer].offset[j];
        pitches[j] = prime->layers[layer].pitch[j];
        modifiers[j] = prime->objects[prime->layers[layer].object_index[j]].drm_format_modifier;
    }

    const vec2i size = { prime->width / layer_format->size_divisor, prime->height / layer_format->size_divisor };
    intptr_t img_attr[44];
    setup_dma_buf_attrs(img_attr, layer_format->drm_format, size.x, size.y, fds, offsets, pitches, modifiers, prime->layers[layer].num_planes, true);

    while(self->params.egl->eglGetError() != EGL_SUCCESS){}
    EGLImage image = self->params.egl->eglCreateImage(self->params.egl->egl_display, 0, EGL_LINUX_DMA_BUF_EXT, NULL, img_attr);
    if(!image) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "import_prime_layer: failed to create egl image from drm fd for output drm fd, error: %d", self->params.egl->eglGetError());
        return false;
    }

    self->params.egl->glBindTexture(GL_TEXTURE_2D, texture);
    self->params.egl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    self->params.egl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    while(self->params.egl->glGetError()) {}
    while(self->params.egl->eglGetError() != EGL_SUCCESS){}
    self->params.egl->glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, image);
    const bool success = self->params.egl->glGetError() == 0 && self->params.egl->eglGetError() == EGL_SUCCESS;
    if(!success) {
        // TODO: Get the error properly
        gsr_log(GSR_LOG_LEVEL_ERROR, "import_prime_layer: failed to bind egl image to gl texture, error: %d", self->params.egl->eglGetError());
    }

    self->params.egl->eglDestroyImage(self->params.egl->egl_display, image);
    self->params.egl->glBindTexture(GL_TEXTURE_2D, 0);
    self->texture_sizes[layer] = size;
    return success;
}

static bool import_surface(gsr_video_encoder_vaapi *self, VASurfaceID surface_id, gsr_vaapi_surface *surface) {
    VADRMPRIMESurfaceDescriptor prime;
    VAStatus va_status = vaExportSurfaceHandle(self->va_dpy, surface_id, VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2, VA_EXPORT_SURFACE_WRITE_ONLY | VA_EXPORT_SURFACE_SEPARATE_LAYERS, &prime);
    if(va_status != VA_STATUS_SUCCESS) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "import_surface: vaExportSurfaceHandle failed, error: %d", va_status);
        return false;
    }
    vaSyncSurface(self->va_dpy, surface_id);

    gsr_video_encoder_vaapi_layer_format layer_formats[2];
    const int num_textures = get_layer_formats(prime.fourcc, layer_formats);
    if(num_textures == 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "import_surface: unexpected fourcc %u for output drm fd, expected nv12, p010, xyuv or y410", prime.fourcc);
        close_prime_fds(&prime);
        return false;
    }

    memset(surface, 0, sizeof(*surface));
    surface->surface_id = surface_id;
    self->params.egl->glGenTextures(num_textures, surface->textures);

    bool success = true;
    for(int i = 0; i < num_textures && success; ++i) {
        success = import_prime_layer(self, &prime, i, &layer_formats[i], surface->textures[i]);
    }

    close_prime_fds(&prime);

    if(!success) {
        self->params.egl->glDeleteTextures(num_textures, surface->textures);
        return false;
    }

    self->num_textures = num_textures;
    return true;
}

static gsr_vaapi_surface* get_surface(gsr_video_encoder_vaapi *self, VASurfaceID surface_id) {
    ++self->num_surface_lookups;
    for(int i = 0; i < self->num_surfaces; ++i) {
        if(self->surfaces[i].surface_id == surface_id) {
            self->surfaces[i].last_used = self->num_surface_lookups;
            return &self->surfaces[i];
        }
    }

    if(self->num_surfaces == MAX_SURFACES) {
        int least_recently_used_index = 0;
        for(int i = 1; i < self->num_surfaces; ++i) {
            if(self->surfaces[i].last_used < self->surfaces[least_recently_used_index].last_used)
                least_recently_used_index = i;
        }

        self->params.egl->glDeleteTextures(self->num_textures, self->surfaces[least_recently_used_index].textures);
        self->surfaces[least_recently_used_index] = self->surfaces[self->num_surfaces - 1];
        --self->num_surfaces;
    }

    gsr_vaapi_surface *surface = &self->surfaces[self->num_surfaces];
    if(!import_surface(self, surface_id, surface))
        return NULL;

    surface->last_used = self->num_surface_lookups;
    ++self->num_surfaces;
    return surface;
}

static bool gsr_video_encoder_vaapi_setup_textures(gsr_video_encoder_vaapi *self, AVCodecContext *video_codec_context, AVFrame *frame) {
    const int res = av_hwframe_get_buffer(video_codec_context->hw_frames_ctx, frame, 0);
    if(res < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_vaapi_setup_textures: av_hwframe_get_buffer failed: %d", res);
        return false;
    }

    return get_surface(self, (uintptr_t)frame->data[3]) != NULL;
}

static void gsr_video_encoder_vaapi_stop(gsr_video_encoder_vaapi *self, AVCodecContext *video_codec_context);

static bool supports_hevc_without_padding(const char *card_path) {
#if LIBAVCODEC_VERSION_INT >= AV_VERSION_INT(61, 28, 100) && VA_CHECK_VERSION(1, 21, 0)
    VAStatus va_status;
    VAConfigID va_config = 0;
    unsigned int num_surface_attr = 0;
    VASurfaceAttrib *surface_attr_list = NULL;
    bool supports_surface_attrib_alignment_size = false;
    int va_major = 0;
    int va_minor = 0;
    bool initialized = false;

    char render_path[128];
    if(!gsr_card_path_get_render_path(card_path, render_path)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to get /dev/dri/renderDXXX file from %s", card_path);
        return false;
    }

    const int drm_fd = open(render_path, O_RDWR);
    if(drm_fd == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to open device %s", render_path);
        return false;
    }

    const VADisplay va_dpy = vaGetDisplayDRM(drm_fd);
    if(!va_dpy) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to get vaapi display for device %s", render_path);
        goto done;
    }

    vaSetInfoCallback(va_dpy, NULL, NULL);

    if(vaInitialize(va_dpy, &va_major, &va_minor) != VA_STATUS_SUCCESS) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: vaInitialize failed");
        goto done;
    }
    initialized = true;

    va_status = vaCreateConfig(va_dpy, VAProfileHEVCMain, VAEntrypointEncSlice, NULL, 0, &va_config);
    if(va_status != VA_STATUS_SUCCESS) {
        va_status = vaCreateConfig(va_dpy, VAProfileHEVCMain, VAEntrypointEncSliceLP, NULL, 0, &va_config);
        if(va_status != VA_STATUS_SUCCESS) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to create hevc vaapi config, error: %s (%d)", vaErrorStr(va_status), va_status);
            return false;
        }
    }

    va_status = vaQuerySurfaceAttributes(va_dpy, va_config, 0, &num_surface_attr);
    if(va_status != VA_STATUS_SUCCESS) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to query vaapi surface attributes size, error: %s (%d)", vaErrorStr(va_status), va_status);
        goto done;
    }

    surface_attr_list = malloc(num_surface_attr * sizeof(VASurfaceAttrib));
    if(!surface_attr_list) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to allocate memory for %u vaapi surface attributes, error: %s (%d)", num_surface_attr, vaErrorStr(va_status), va_status);
        goto done;
    }

    va_status = vaQuerySurfaceAttributes(va_dpy, va_config, surface_attr_list, &num_surface_attr);
    if(va_status != VA_STATUS_SUCCESS) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "supports_hevc_without_padding: failed to query vaapi surface attributes data, error: %s (%d)", vaErrorStr(va_status), va_status);
        goto done;
    }

    for(unsigned int i = 0; i < num_surface_attr; ++i) {
        if(surface_attr_list[i].type == VASurfaceAttribAlignmentSize) {
            supports_surface_attrib_alignment_size = true;
            break;
        }
    }

    done:
    free(surface_attr_list);
    if(va_config > 0)
        vaDestroyConfig(va_dpy, va_config);
    if(initialized)
        vaTerminate(va_dpy);
    if(drm_fd > 0)
        close(drm_fd);
    return supports_surface_attrib_alignment_size;
#else
    return false;
#endif
}

static bool gsr_video_encoder_vaapi_start(gsr_video_encoder *encoder, AVCodecContext *video_codec_context, AVFrame *frame) {
    gsr_video_encoder_vaapi *self = encoder->priv;

    if(self->params.egl->gpu_info.vendor == GSR_GPU_VENDOR_AMD && video_codec_context->codec_id == AV_CODEC_ID_HEVC) {
        if(supports_hevc_without_padding(self->params.egl->card_path)) {
            video_codec_context->width = FFALIGN(video_codec_context->width, 2);
            video_codec_context->height = FFALIGN(video_codec_context->height, 2);
        } else {
            video_codec_context->width = FFALIGN(video_codec_context->width, 64);
            video_codec_context->height = FFALIGN(video_codec_context->height, 16);
        }
    } else if(self->params.egl->gpu_info.vendor == GSR_GPU_VENDOR_AMD && video_codec_context->codec_id == AV_CODEC_ID_AV1) {
        // TODO: Dont do this for VCN 5 and forward which should fix this hardware bug
        video_codec_context->width = FFALIGN(video_codec_context->width, 64);
        // AMD driver has special case handling for 1080 height to set it to 1082 instead of 1088 (1080 aligned to 16).
        // TODO: Set height to 1082 in this case, but it wont work because it will be aligned to 1088.
        if(video_codec_context->height == 1080) {
            video_codec_context->height = 1080;
        } else {
            video_codec_context->height = FFALIGN(video_codec_context->height, 16);
        }
    } else {
        video_codec_context->width = FFALIGN(video_codec_context->width, 2);
        video_codec_context->height = FFALIGN(video_codec_context->height, 2);
    }

    if(FFALIGN(video_codec_context->width, 2) != FFALIGN(frame->width, 2) || FFALIGN(video_codec_context->height, 2) != FFALIGN(frame->height, 2)) {
        gsr_log(GSR_LOG_LEVEL_WARNING, "gsr_video_encoder_vaapi_start: black bars have been added to the video because of a bug in AMD drivers/hardware. Record with h264/hevc codec instead (-k h264) to get around this issue");
    }

    if(video_codec_context->width < 128)
        video_codec_context->width = 128;

    if(video_codec_context->height < 128)
        video_codec_context->height = 128;

    frame->width = video_codec_context->width;
    frame->height = video_codec_context->height;

    if(!gsr_video_encoder_vaapi_setup_context(self, video_codec_context)) {
        gsr_video_encoder_vaapi_stop(self, video_codec_context);
        return false;
    }

    if(!gsr_video_encoder_vaapi_setup_textures(self, video_codec_context, frame)) {
        gsr_video_encoder_vaapi_stop(self, video_codec_context);
        return false;
    }

    return true;
}

void gsr_video_encoder_vaapi_stop(gsr_video_encoder_vaapi *self, AVCodecContext *video_codec_context) {
    for(int i = 0; i < self->num_surfaces; ++i) {
        self->params.egl->glDeleteTextures(self->num_textures, self->surfaces[i].textures);
    }
    self->num_surfaces = 0;

    if(video_codec_context && video_codec_context->hw_frames_ctx)
        av_buffer_unref(&video_codec_context->hw_frames_ctx);
    if(self->device_ctx)
        av_buffer_unref(&self->device_ctx);
}

static void gsr_video_encoder_vaapi_prepare_frame(gsr_video_encoder *encoder, AVFrame *frame, gsr_color_conversion *color_conversion) {
    gsr_video_encoder_vaapi *self = encoder->priv;

    AVFrame *new_frame = gsr_hw_frame_alloc(frame->hw_frames_ctx);
    if(!new_frame)
        return;

    gsr_vaapi_surface *surface = get_surface(self, (uintptr_t)new_frame->data[3]);
    if(surface) {
        gsr_frame_move_hw_buffer(frame, new_frame);
        gsr_color_conversion_set_destination_textures(color_conversion, surface->textures, &surface->clear_generation);
    }
    av_frame_free(&new_frame);
}

static void gsr_video_encoder_vaapi_get_textures(gsr_video_encoder *encoder, unsigned int *textures, vec2i *texture_sizes, int *num_textures, gsr_destination_color *destination_color) {
    gsr_video_encoder_vaapi *self = encoder->priv;
    for(int i = 0; i < self->num_textures; ++i) {
        textures[i] = self->surfaces[0].textures[i];
        texture_sizes[i] = self->texture_sizes[i];
    }
    *num_textures = self->num_textures;
    *destination_color = gsr_video_encoder_vaapi_get_destination_color(&self->params);
}

static void gsr_video_encoder_vaapi_destroy(gsr_video_encoder *encoder, AVCodecContext *video_codec_context) {
    gsr_video_encoder_vaapi_stop(encoder->priv, video_codec_context);
    free(encoder->priv);
    free(encoder);
}

gsr_video_encoder* gsr_video_encoder_vaapi_create(const gsr_video_encoder_vaapi_params *params) {
    gsr_video_encoder *encoder = calloc(1, sizeof(gsr_video_encoder));
    if(!encoder)
        return NULL;

    gsr_video_encoder_vaapi *encoder_vaapi = calloc(1, sizeof(gsr_video_encoder_vaapi));
    if(!encoder_vaapi) {
        free(encoder);
        return NULL;
    }

    encoder_vaapi->params = *params;

    *encoder = (gsr_video_encoder) {
        .start = gsr_video_encoder_vaapi_start,
        .prepare_frame = gsr_video_encoder_vaapi_prepare_frame,
        .get_textures = gsr_video_encoder_vaapi_get_textures,
        .destroy = gsr_video_encoder_vaapi_destroy,
        .priv = encoder_vaapi
    };

    return encoder;
}
