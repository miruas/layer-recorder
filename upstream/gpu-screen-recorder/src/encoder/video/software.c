#include "../../../include/encoder/video/software.h"
#include "../../../include/log.h"
#include "../../../include/egl.h"
#include "../../../include/utils.h"

#include <libavcodec/avcodec.h>
#include <libavutil/frame.h>

#include <stdlib.h>

#define LINESIZE_ALIGNMENT 4

typedef struct {
    gsr_video_encoder_software_params params;
    gsr_video_encoder_texture_format texture_format;

    unsigned int target_textures[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
    vec2i texture_sizes[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
} gsr_video_encoder_software;

static gsr_destination_color gsr_video_encoder_software_get_destination_color(const gsr_video_encoder_software_params *params) {
    if(params->yuv444)
        return GSR_DESTINATION_COLOR_YUV444P;
    else
        return params->color_depth == GSR_COLOR_DEPTH_10_BITS ? GSR_DESTINATION_COLOR_P010 : GSR_DESTINATION_COLOR_NV12;
}

static bool gsr_video_encoder_software_setup_textures(gsr_video_encoder_software *self, AVCodecContext *video_codec_context, AVFrame *frame) {
    int res = av_frame_get_buffer(frame, LINESIZE_ALIGNMENT);
    if(res < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_software_setup_textures: av_frame_get_buffer failed: %d", res);
        return false;
    }

    res = av_frame_make_writable(frame);
    if(res < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_video_encoder_software_setup_textures: av_frame_make_writable failed: %d", res);
        return false;
    }

    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        const gsr_video_encoder_plane_format *plane = &self->texture_format.planes[i];
        self->texture_sizes[i] = (vec2i){ video_codec_context->width / plane->size_divisor, video_codec_context->height / plane->size_divisor };
        self->target_textures[i] = gl_create_texture(self->params.egl, self->texture_sizes[i].x, self->texture_sizes[i].y, plane->internal_format, plane->format, GL_NEAREST);
        if(self->target_textures[i] == 0) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_capture_kms_setup_cuda_textures: failed to create opengl texture");
            return false;
        }
    }

    return true;
}

static void gsr_video_encoder_software_stop(gsr_video_encoder_software *self, AVCodecContext *video_codec_context);

static bool gsr_video_encoder_software_start(gsr_video_encoder *encoder, AVCodecContext *video_codec_context, AVFrame *frame) {
    gsr_video_encoder_software *self = encoder->priv;

    video_codec_context->width = FFALIGN(video_codec_context->width, LINESIZE_ALIGNMENT);
    video_codec_context->height = FFALIGN(video_codec_context->height, 2);

    frame->width = video_codec_context->width;
    frame->height = video_codec_context->height;

    if(!gsr_video_encoder_software_setup_textures(self, video_codec_context, frame)) {
        gsr_video_encoder_software_stop(self, video_codec_context);
        return false;
    }

    return true;
}

void gsr_video_encoder_software_stop(gsr_video_encoder_software *self, AVCodecContext *video_codec_context) {
    (void)video_codec_context;
    self->params.egl->glDeleteTextures(GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES, self->target_textures);
    for(int i = 0; i < GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES; ++i) {
        self->target_textures[i] = 0;
    }
}

static void gsr_video_encoder_software_copy_textures_to_frame(gsr_video_encoder *encoder, AVFrame *frame, gsr_color_conversion *color_conversion) {
    gsr_video_encoder_software *self = encoder->priv;
    // TODO: hdr support
    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        const gsr_video_encoder_plane_format *plane = &self->texture_format.planes[i];
        // TODO: Use glPixelStore?
        gsr_color_conversion_read_destination_texture(color_conversion, i, 0, 0, frame->width / plane->size_divisor, frame->height / plane->size_divisor, plane->format, GL_UNSIGNED_BYTE, frame->data[i]);
    }
    // cap_kms->kms.base.egl->eglSwapBuffers(cap_kms->kms.base.egl->egl_display, cap_kms->kms.base.egl->egl_surface);

    //self->params.egl->glFlush();
    //self->params.egl->glFinish();
}

static void gsr_video_encoder_software_get_textures(gsr_video_encoder *encoder, unsigned int *textures, vec2i *texture_sizes, int *num_textures, gsr_destination_color *destination_color) {
    gsr_video_encoder_software *self = encoder->priv;
    for(int i = 0; i < self->texture_format.num_planes; ++i) {
        textures[i] = self->target_textures[i];
        texture_sizes[i] = self->texture_sizes[i];
    }
    *num_textures = self->texture_format.num_planes;
    *destination_color = self->texture_format.destination_color;
}

static void gsr_video_encoder_software_destroy(gsr_video_encoder *encoder, AVCodecContext *video_codec_context) {
    gsr_video_encoder_software_stop(encoder->priv, video_codec_context);
    free(encoder->priv);
    free(encoder);
}

gsr_video_encoder* gsr_video_encoder_software_create(const gsr_video_encoder_software_params *params) {
    gsr_video_encoder *encoder = calloc(1, sizeof(gsr_video_encoder));
    if(!encoder)
        return NULL;

    gsr_video_encoder_software *encoder_software = calloc(1, sizeof(gsr_video_encoder_software));
    if(!encoder_software) {
        free(encoder);
        return NULL;
    }

    encoder_software->params = *params;
    encoder_software->texture_format = gsr_video_encoder_get_texture_format(gsr_video_encoder_software_get_destination_color(params));

    *encoder = (gsr_video_encoder) {
        .start = gsr_video_encoder_software_start,
        .copy_textures_to_frame = gsr_video_encoder_software_copy_textures_to_frame,
        .get_textures = gsr_video_encoder_software_get_textures,
        .destroy = gsr_video_encoder_software_destroy,
        .priv = encoder_software
    };

    return encoder;
}
