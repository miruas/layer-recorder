#ifndef GSR_COLOR_CONVERSION_H
#define GSR_COLOR_CONVERSION_H

#include "shader.h"
#include "defs.h"
#include "vec2.h"
#include <stdbool.h>
#include <stdint.h>

#define GSR_COLOR_CONVERSION_MAX_GRAPHICS_SHADERS 18
#define GSR_COLOR_CONVERSION_MAX_FRAMEBUFFERS 2
#define GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES 3

typedef enum {
    GSR_COLOR_MATRIX_TRANSFER_PQ           = 1,
    GSR_COLOR_MATRIX_TRANSFER_GAMMA22      = 2,
    GSR_COLOR_MATRIX_TRANSFER_GAMMA22_TO_PQ = 3
} gsr_color_matrix_transfer;

typedef enum {
    GSR_SOURCE_COLOR_RGB,
    GSR_SOURCE_COLOR_BGR,
    GSR_SOURCE_COLOR_YUYV
} gsr_source_color;

typedef enum {
    GSR_DESTINATION_COLOR_NV12,      /* YUV420, 8-bit */
    GSR_DESTINATION_COLOR_P010,      /* YUV420, 10-bit */
    GSR_DESTINATION_COLOR_NV24,      /* YUV444 semi-planar, 8-bit */
    GSR_DESTINATION_COLOR_P410,      /* YUV444 semi-planar, 10-bit */
    GSR_DESTINATION_COLOR_YUV444P,   /* YUV444 planar, 8-bit */
    GSR_DESTINATION_COLOR_YUV444P16, /* YUV444 planar, 10-bit */
    GSR_DESTINATION_COLOR_VUYX,      /* YUV444 packed, 8-bit */
    GSR_DESTINATION_COLOR_XV30,      /* YUV444 packed, 10-bit */
    GSR_DESTINATION_COLOR_RGB
} gsr_destination_color;

typedef enum {
    GSR_YUV_MATRIX_BT709,
    GSR_YUV_MATRIX_BT2020
} gsr_yuv_matrix;

typedef enum {
    GSR_ROT_0,
    GSR_ROT_90,
    GSR_ROT_180,
    GSR_ROT_270
} gsr_rotation;

typedef enum {
    GSR_FLIP_NONE       = 0,
    GSR_FLIP_HORIZONTAL = (1 << 0),
    GSR_FLIP_VERTICAL   = (1 << 1)
} gsr_flip;

typedef struct {
    int rotation_matrix;
    int offset;
    int hdr_sdr_white_scale;
    int hdr_relative_peak_luminance;
    int gamma_lut_enabled;
    int color_matrix_transfer;
    int color_matrix;
} gsr_color_graphics_uniforms;

typedef struct {
    gsr_egl *egl;

    gsr_destination_color destination_color;

    unsigned int destination_textures[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
    vec2i destination_textures_size[GSR_COLOR_CONVERSION_MAX_DESTINATION_TEXTURES];
    int num_destination_textures;

    gsr_color_range color_range;
    gsr_yuv_matrix yuv_matrix;
    bool load_external_image_shader;
} gsr_color_conversion_params;

typedef struct {
    gsr_color_conversion_params params;
    float rgb_to_yuv_matrix[16];

    gsr_color_graphics_uniforms graphics_uniforms[GSR_COLOR_CONVERSION_MAX_GRAPHICS_SHADERS];
    gsr_shader graphics_shaders[GSR_COLOR_CONVERSION_MAX_GRAPHICS_SHADERS];

    unsigned int framebuffers[GSR_COLOR_CONVERSION_MAX_FRAMEBUFFERS];

    unsigned int vertex_array_object_id;
    unsigned int vertex_buffer_object_id;

    bool schedule_clear;
    uint64_t clear_generation;
    float hdr_sdr_white_scale;
    float hdr_relative_peak_luminance;
    unsigned int gamma_lut_texture_id;
    bool gamma_lut_apply;
    int color_matrix_transfer;
    float color_matrix[9];
    bool hdr_shaders_loaded;
    bool hdr_shaders_load_failed;
} gsr_color_conversion;

int gsr_color_conversion_init(gsr_color_conversion *self, const gsr_color_conversion_params *params);
void gsr_color_conversion_deinit(gsr_color_conversion *self);

void gsr_color_conversion_draw(gsr_color_conversion *self, unsigned int texture_id, vec2i destination_pos, vec2i destination_size, vec2i source_pos, vec2i source_size, vec2i texture_size, gsr_rotation rotation, gsr_flip flip, gsr_source_color source_color, bool external_texture);
void gsr_color_conversion_clear(gsr_color_conversion *self);
void gsr_color_conversion_set_destination_textures(gsr_color_conversion *self, const unsigned int *destination_textures, uint64_t *destination_clear_generation);
void gsr_color_conversion_set_hdr_to_sdr_tone_mapping(gsr_color_conversion *self, bool enable, float hdr_peak_luminance, float sdr_white_luminance);
void gsr_color_conversion_set_gamma_lut(gsr_color_conversion *self, const float *rgb_values, int num_entries);
void gsr_color_conversion_enable_gamma_lut(gsr_color_conversion *self, bool enable);
/* |linear_rgb_matrix| is a row major 3x3 matrix that is applied to linear rgb values in the draw functions, for RGB/BGR sources. NULL to disable.
   |transfer| chooses the transfer characteristics used to decode/encode the source image around the matrix multiply */
void gsr_color_conversion_set_color_matrix(gsr_color_conversion *self, const float *linear_rgb_matrix, gsr_color_matrix_transfer transfer);
void gsr_color_conversion_read_destination_texture(gsr_color_conversion *self, int destination_texture_index, int x, int y, int width, int height, unsigned int color_format, unsigned int data_format, void *pixels);

gsr_rotation gsr_monitor_rotation_to_rotation(gsr_monitor_rotation monitor_rotation);

#endif /* GSR_COLOR_CONVERSION_H */
