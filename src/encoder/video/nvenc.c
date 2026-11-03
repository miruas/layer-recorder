#include "../../../include/encoder/video/nvenc.h"
#include "../../../include/log.h"
#include "../../../include/egl.h"
#include "../../../include/cuda.h"
#include "../../../include/utils.h"
#include "../../../include/ffmpeg_utils.h"

#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext_cuda.h>

#include <stdlib.h>

typedef struct {
    gsr_video_encoder_nvenc_params params;
    enum AVPixelFormat sw_format;
    gsr_video_encoder_texture_format texture_format;

    unsigned int target_textures[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
    vec2i target_texture_size[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];

    AVBufferRef *device_ctx;

    gsr_cuda cuda;
    CUgraphicsResource cuda_graphics_resources[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
    CUstream cuda_stream;
} gsr_video_encoder_nvenc;

static enum AVPixelFormat gsr_video_encoder_nvenc_get_sw_format(const gsr_video_encoder_nvenc_params *params) {
    const bool ten_bit = params->color_depth == GSR_COLOR_DEPTH_10_BITS;
    if(params->yuv444)
        return ten_bit ? AV_PIX_FMT_YUV444P16LE : AV_PIX_FMT_YUV444P;
    else
        return ten_bit ? AV_PIX_FMT_P010LE : AV_PIX_FMT_NV12;
}

static gsr_destination_color gsr_video_encoder_nvenc_get_destination_color(const gsr_video_encoder_nvenc_params *params) {
    const bool ten_bit = params->color_depth == GSR_COLOR_DEPTH_10_BITS;
    if(params->yuv444)
        return ten_bit ? GSR_DESTINATION_COLOR_YUV444P16 : GSR_DESTINATION_COLOR_YUV444P;
    else
        return ten_bit ? GSR_DESTINATION_COLOR_P010 : GSR_DESTINATION_COLOR_NV12;
}

static bool gsr_video_encoder_nvenc_setup_context(gsr_video_encoder_nvenc *self, AVCodecContext *video_codec_context) {
    self->device_ctx = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_CUDA);
    if(!self->device_ctx) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_context failed: failed to create hardware device context");
        return false;
    }

    AVHWDeviceContext *hw_device_context = (AVHWDeviceContext*)self->device_ctx->data;
    AVCUDADeviceContext *cuda_device_context = (AVCUDADeviceContext*)hw_device_context->hwctx;
    cuda_device_context->cuda_ctx = self->cuda.cu_ctx;
    if(av_hwdevice_ctx_init(self->device_ctx) < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_context failed: failed to create hardware device context");
        av_buffer_unref(&self->device_ctx);
        return false;
    }

    AVBufferRef *frame_context = av_hwframe_ctx_alloc(self->device_ctx);
    if(!frame_context) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_context failed: failed to create hwframe context");
        av_buffer_unref(&self->device_ctx);
        return false;
    }

    AVHWFramesContext *hw_frame_context = (AVHWFramesContext*)frame_context->data;
    hw_frame_context->width = video_codec_context->width;
    hw_frame_context->height = video_codec_context->height;
    hw_frame_context->sw_format = self->sw_format;
    hw_frame_context->format = video_codec_context->pix_fmt;
    hw_frame_context->device_ctx = (AVHWDeviceContext*)self->device_ctx->data;

    if (av_hwframe_ctx_init(frame_context) < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_context failed: failed to initialize hardware frame context (note: ffmpeg version needs to be > 4.0)");
        av_buffer_unref(&self->device_ctx);
        //av_buffer_unref(&frame_context);
        return false;
    }

    self->cuda_stream = cuda_device_context->stream;
    video_codec_context->hw_frames_ctx = av_buffer_ref(frame_context);
    av_buffer_unref(&frame_context);
    return true;
}

static bool cuda_register_opengl_texture(gsr_cuda *cuda, CUgraphicsResource *cuda_graphics_resource, unsigned int texture_id) {
    CUresult res;
    res = cuda->cuGraphicsGLRegisterImage(cuda_graphics_resource, texture_id, GL_TEXTURE_2D, CU_GRAPHICS_REGISTER_FLAGS_NONE);
    if (res != CUDA_SUCCESS) {
        const char *err_str = "unknown";
        cuda->cuGetErrorString(res, &err_str);
        gsr_log(GSR_LOG_LEVEL_ERROR, "cuda_register_opengl_texture: cuGraphicsGLRegisterImage failed, error: %s, texture id: %u", err_str, texture_id);
        return false;
    }

    cuda->cuGraphicsResourceSetMapFlags(*cuda_graphics_resource, CU_GRAPHICS_MAP_RESOURCE_FLAGS_NONE);
    return true;
}

static bool gsr_video_encoder_nvenc_setup_textures(gsr_video_encoder_nvenc *self, AVCodecContext *video_codec_context, AVFrame *frame) {
    const int res = av_hwframe_get_buffer(video_codec_context->hw_frames_ctx, frame, 0);
    if(res < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_textures: av_hwframe_get_buffer failed: %d", res);
        return false;
    }

    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        const gsr_video_encoder_plane_format *plane = &self->texture_format.planes[i];
        self->target_texture_size[i] = (vec2i){ video_codec_context->width / plane->size_divisor, video_codec_context->height / plane->size_divisor };
        self->target_textures[i] = gl_create_texture(self->params.egl, self->target_texture_size[i].x, self->target_texture_size[i].y, plane->internal_format, plane->format, GL_NEAREST);
        if(self->target_textures[i] == 0) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_setup_textures: failed to create opengl texture");
            return false;
        }

        if(!cuda_register_opengl_texture(&self->cuda, &self->cuda_graphics_resources[i], self->target_textures[i])) {
            return false;
        }
    }

    return true;
}

static void gsr_video_encoder_nvenc_stop(gsr_video_encoder_nvenc *self, AVCodecContext *video_codec_context);

static bool gsr_video_encoder_nvenc_start(gsr_video_encoder *encoder, AVCodecContext *video_codec_context, AVFrame *frame) {
    gsr_video_encoder_nvenc *self = encoder->priv;

    if(!gsr_cuda_load(&self->cuda, self->params.egl->card_path)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_start: failed to load cuda");
        gsr_video_encoder_nvenc_stop(self, video_codec_context);
        return false;
    }

    video_codec_context->width = FFALIGN(video_codec_context->width, 2);
    video_codec_context->height = FFALIGN(video_codec_context->height, 2);

    if(video_codec_context->width < 128)
        video_codec_context->width = 128;

    if(video_codec_context->height < 128)
        video_codec_context->height = 128;

    frame->width = video_codec_context->width;
    frame->height = video_codec_context->height;

    if(!gsr_video_encoder_nvenc_setup_context(self, video_codec_context)) {
        gsr_video_encoder_nvenc_stop(self, video_codec_context);
        return false;
    }

    if(!gsr_video_encoder_nvenc_setup_textures(self, video_codec_context, frame)) {
        gsr_video_encoder_nvenc_stop(self, video_codec_context);
        return false;
    }

    return true;
}

void gsr_video_encoder_nvenc_stop(gsr_video_encoder_nvenc *self, AVCodecContext *video_codec_context) {
    self->params.egl->glDeleteTextures(GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES, self->target_textures);
    for(int i = 0; i < GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES; ++i) {
        self->target_textures[i] = 0;
    }

    if(video_codec_context && video_codec_context->hw_frames_ctx)
        av_buffer_unref(&video_codec_context->hw_frames_ctx);
    if(self->device_ctx)
        av_buffer_unref(&self->device_ctx);

    if(self->cuda.cu_ctx) {
        for(int i = 0; i < GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES; ++i) {
            if(self->cuda_graphics_resources[i]) {
                self->cuda.cuGraphicsUnregisterResource(self->cuda_graphics_resources[i]);
                self->cuda_graphics_resources[i] = 0;
            }
        }
    }

    gsr_cuda_unload(&self->cuda);
}

static void gsr_video_encoder_nvenc_copy_textures_to_frame(gsr_video_encoder *encoder, AVFrame *frame, gsr_color_conversion *color_conversion) {
    gsr_video_encoder_nvenc *self = encoder->priv;

    /*
        Mapping the textures makes the copy wait for the opengl rendering to finish and unmapping them makes opengl wait for the copy to finish, on the gpu.
        The encoder waits for the copy on the gpu since FFmpeg gives the encoder the same cuda stream (nvEncSetIOCudaStreams).
    */
    const CUresult map_result = self->cuda.cuGraphicsMapResources(self->texture_format.num_planes, self->cuda_graphics_resources, self->cuda_stream);
    if(map_result != CUDA_SUCCESS) {
        const char *err_str = "unknown";
        self->cuda.cuGetErrorString(map_result, &err_str);
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_nvenc_copy_textures_to_frame: cuGraphicsMapResources failed, error: %s", err_str);
        return;
    }

    gsr_frame_replace_hw_buffer(frame);

    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        CUarray mapped_array = NULL;
        if(self->cuda.cuGraphicsSubResourceGetMappedArray(&mapped_array, self->cuda_graphics_resources[i], 0, 0) != CUDA_SUCCESS)
            continue;

        CUDA_MEMCPY2D memcpy_struct;
        memcpy_struct.srcXInBytes = 0;
        memcpy_struct.srcY = 0;
        memcpy_struct.srcMemoryType = CU_MEMORYTYPE_ARRAY;

        memcpy_struct.dstXInBytes = 0;
        memcpy_struct.dstY = 0;
        memcpy_struct.dstMemoryType = CU_MEMORYTYPE_DEVICE;

        memcpy_struct.srcArray = mapped_array;
        memcpy_struct.srcPitch = self->target_texture_size[i].x;
        memcpy_struct.dstDevice = (CUdeviceptr)frame->data[i];
        memcpy_struct.dstPitch = frame->linesize[i];
        memcpy_struct.WidthInBytes = self->target_texture_size[i].x * self->texture_format.planes[i].bytes_per_pixel;
        memcpy_struct.Height = self->target_texture_size[i].y;
        // TODO: Remove this copy if possible
        self->cuda.cuMemcpy2DAsync_v2(&memcpy_struct, self->cuda_stream);
    }

    self->cuda.cuGraphicsUnmapResources(self->texture_format.num_planes, self->cuda_graphics_resources, self->cuda_stream);
}

static void gsr_video_encoder_nvenc_get_textures(gsr_video_encoder *encoder, unsigned int *textures, vec2i *texture_sizes, int *num_textures, gsr_destination_color *destination_color) {
    gsr_video_encoder_nvenc *self = encoder->priv;
    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        textures[i] = self->target_textures[i];
        texture_sizes[i] = self->target_texture_size[i];
    }
    *num_textures = self->texture_format.num_planes;
    *destination_color = self->texture_format.destination_color;
}

static void gsr_video_encoder_nvenc_destroy(gsr_video_encoder *encoder, AVCodecContext *video_codec_context) {
    gsr_video_encoder_nvenc_stop(encoder->priv, video_codec_context);
    free(encoder->priv);
    free(encoder);
}

gsr_video_encoder* gsr_video_encoder_nvenc_create(const gsr_video_encoder_nvenc_params *params) {
    gsr_video_encoder *encoder = calloc(1, sizeof(gsr_video_encoder));
    if(!encoder)
        return NULL;

    gsr_video_encoder_nvenc *encoder_cuda = calloc(1, sizeof(gsr_video_encoder_nvenc));
    if(!encoder_cuda) {
        free(encoder);
        return NULL;
    }

    encoder_cuda->params = *params;
    encoder_cuda->sw_format = gsr_video_encoder_nvenc_get_sw_format(params);
    encoder_cuda->texture_format = gsr_video_encoder_get_texture_format(gsr_video_encoder_nvenc_get_destination_color(params));

    *encoder = (gsr_video_encoder) {
        .start = gsr_video_encoder_nvenc_start,
        .copy_textures_to_frame = gsr_video_encoder_nvenc_copy_textures_to_frame,
        .get_textures = gsr_video_encoder_nvenc_get_textures,
        .destroy = gsr_video_encoder_nvenc_destroy,
        .priv = encoder_cuda
    };

    return encoder;
}
