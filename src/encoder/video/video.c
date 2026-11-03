#include "../../../include/encoder/video/video.h"
#include "../../../include/egl.h"

#include <assert.h>

bool gsr_video_encoder_start(gsr_video_encoder *encoder, AVCodecContext *video_codec_context, AVFrame *frame) {
    assert(!encoder->started);
    bool res = encoder->start(encoder, video_codec_context, frame);
    if(res)
        encoder->started = true;
    return res;
}

void gsr_video_encoder_destroy(gsr_video_encoder *encoder, AVCodecContext *video_codec_context) {
    assert(encoder->started);
    encoder->started = false;
    encoder->destroy(encoder, video_codec_context);
}

void gsr_video_encoder_prepare_frame(gsr_video_encoder *encoder, AVFrame *frame, gsr_color_conversion *color_conversion) {
    assert(encoder->started);
    if(encoder->prepare_frame)
        encoder->prepare_frame(encoder, frame, color_conversion);
}

void gsr_video_encoder_copy_textures_to_frame(gsr_video_encoder *encoder, AVFrame *frame, gsr_color_conversion *color_conversion) {
    assert(encoder->started);
    if(encoder->copy_textures_to_frame)
        encoder->copy_textures_to_frame(encoder, frame, color_conversion);
}

void gsr_video_encoder_get_textures(gsr_video_encoder *encoder, unsigned int *textures, vec2i *texture_sizes, int *num_textures, gsr_destination_color *destination_color) {
    assert(encoder->started);
    encoder->get_textures(encoder, textures, texture_sizes, num_textures, destination_color);
}

gsr_video_encoder_texture_format gsr_video_encoder_get_texture_format(gsr_destination_color destination_color) {
    const gsr_video_encoder_plane_format r8 = { .internal_format = GL_R8, .format = GL_RED, .size_divisor = 1, .bytes_per_pixel = 1 };
    const gsr_video_encoder_plane_format r16 = { .internal_format = GL_R16, .format = GL_RED, .size_divisor = 1, .bytes_per_pixel = 2 };
    const gsr_video_encoder_plane_format rg8_subsampled = { .internal_format = GL_RG8, .format = GL_RG, .size_divisor = 2, .bytes_per_pixel = 2 };
    const gsr_video_encoder_plane_format rg16_subsampled = { .internal_format = GL_RG16, .format = GL_RG, .size_divisor = 2, .bytes_per_pixel = 4 };

    switch(destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
            return (gsr_video_encoder_texture_format){ .destination_color = destination_color, .planes = { r8, rg8_subsampled }, .num_planes = 2 };
        case GSR_DESTINATION_COLOR_P010:
            return (gsr_video_encoder_texture_format){ .destination_color = destination_color, .planes = { r16, rg16_subsampled }, .num_planes = 2 };
        case GSR_DESTINATION_COLOR_YUV444P:
            return (gsr_video_encoder_texture_format){ .destination_color = destination_color, .planes = { r8, r8, r8 }, .num_planes = 3 };
        case GSR_DESTINATION_COLOR_YUV444P16:
            return (gsr_video_encoder_texture_format){ .destination_color = destination_color, .planes = { r16, r16, r16 }, .num_planes = 3 };
        default:
            return (gsr_video_encoder_texture_format){ .destination_color = destination_color, .num_planes = 0 };
    }
}
