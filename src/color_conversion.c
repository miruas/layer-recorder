#include "../include/color_conversion.h"
#include "../include/log.h"
#include "../include/egl.h"
#include <stdio.h>
#include <string.h>
#include <assert.h>

#define GRAPHICS_SHADER_INDEX_Y                    0
#define GRAPHICS_SHADER_INDEX_UV                   1
#define GRAPHICS_SHADER_INDEX_Y_EXTERNAL           2
#define GRAPHICS_SHADER_INDEX_UV_EXTERNAL          3
#define GRAPHICS_SHADER_INDEX_RGB                  4
#define GRAPHICS_SHADER_INDEX_RGB_EXTERNAL         5
#define GRAPHICS_SHADER_INDEX_YUYV_TO_Y            6
#define GRAPHICS_SHADER_INDEX_YUYV_TO_UV           7
#define GRAPHICS_SHADER_INDEX_YUYV_TO_Y_EXTERNAL   8
#define GRAPHICS_SHADER_INDEX_YUYV_TO_UV_EXTERNAL  9
#define GRAPHICS_SHADER_INDEX_YUYV_TO_RGB          10
#define GRAPHICS_SHADER_INDEX_YUYV_TO_RGB_EXTERNAL 11
#define GRAPHICS_SHADER_INDEX_Y_HDR                12
#define GRAPHICS_SHADER_INDEX_UV_HDR               13
#define GRAPHICS_SHADER_INDEX_Y_HDR_EXTERNAL       14
#define GRAPHICS_SHADER_INDEX_UV_HDR_EXTERNAL      15
#define GRAPHICS_SHADER_INDEX_RGB_HDR              16
#define GRAPHICS_SHADER_INDEX_RGB_HDR_EXTERNAL     17

#define SDR_WHITE_LUMINANCE_DEFAULT 203.0f /* ITU-R BT.2408 */

#define HDR_TO_SDR_TONE_MAPPING \
    "uniform float hdr_sdr_white_scale;                                                              \n" \
    "uniform float hdr_relative_peak_luminance;                                                      \n" \
    "const float HDR_TO_SDR_KNEE = 0.9;                                                              \n" \
    "const float PQ_M1 = 0.1593017578125;                                                            \n" \
    "const float PQ_M2 = 78.84375;                                                                   \n" \
    "const float PQ_C1 = 0.8359375;                                                                  \n" \
    "const float PQ_C2 = 18.8515625;                                                                 \n" \
    "const float PQ_C3 = 18.6875;                                                                    \n" \
    "const mat3 BT2020toBT709 = mat3(1.660491, -0.124550, -0.018151,                                 \n" \
    "                               -0.587641,  1.132900, -0.100579,                                 \n" \
    "                               -0.072850, -0.008349,  1.118730);                                \n" \
    "vec3 pq_to_luminance3(vec3 pq) {                                                                \n" \
    "    vec3 e = pow(max(pq, vec3(0.0)), vec3(1.0/PQ_M2));                                          \n" \
    "    return pow(max(e - PQ_C1, vec3(0.0)) / (PQ_C2 - PQ_C3*e), vec3(1.0/PQ_M1));                 \n" \
    "}                                                                                               \n" \
    "vec3 luminance3_to_pq(vec3 luminance) {                                                         \n" \
    "    vec3 l = pow(clamp(luminance, vec3(0.0), vec3(1.0)), vec3(PQ_M1));                          \n" \
    "    return pow((PQ_C1 + PQ_C2*l) / (1.0 + PQ_C3*l), vec3(PQ_M2));                               \n" \
    "}                                                                                               \n" \
    "vec3 tone_map_hdr_to_sdr(vec3 pq_rgb) {                                                         \n" \
    "    vec3 color = BT2020toBT709 * pq_to_luminance3(pq_rgb) * hdr_sdr_white_scale;                \n" \
    "    float luminance = dot(color, vec3(0.2126, 0.7152, 0.0722));                                 \n" \
    "    if(luminance > HDR_TO_SDR_KNEE) {                                                           \n" \
    "        float rolloff = 1.0/(1.0 - HDR_TO_SDR_KNEE) - 1.0/(hdr_relative_peak_luminance - HDR_TO_SDR_KNEE); \n" \
    "        float excess = luminance - HDR_TO_SDR_KNEE;                                             \n" \
    "        color *= (HDR_TO_SDR_KNEE + excess/(1.0 + excess*rolloff)) / luminance;                 \n" \
    "    }                                                                                           \n" \
    "    return pow(clamp(color, 0.0, 1.0), vec3(1.0/2.2));                                          \n" \
    "}                                                                                               \n"

#define COLOR_MATRIX_GLSL \
    "uniform int color_matrix_transfer;                                                               \n" \
    "uniform mat3 color_matrix;                                                                \n" \
    "vec3 apply_color_matrix(vec3 color) {                                                          \n" \
    "    if(color_matrix_transfer == 1) {                                                            \n" \
    "        vec3 luminance = max(color_matrix * pq_to_luminance3(color), vec3(0.0));                \n" \
    "        return luminance3_to_pq(luminance);                                                     \n" \
    "    } else if(color_matrix_transfer == 2) {                                                     \n" \
    "        vec3 linear_color = pow(max(color, vec3(0.0)), vec3(2.2));                              \n" \
    "        linear_color = clamp(color_matrix * linear_color, vec3(0.0), vec3(1.0));                \n" \
    "        return pow(linear_color, vec3(1.0/2.2));                                                \n" \
    "    } else {                                                                                    \n" \
    "        vec3 linear_color = pow(max(color, vec3(0.0)), vec3(2.2));                              \n" \
    "        return luminance3_to_pq(max(color_matrix * linear_color, vec3(0.0)));                   \n" \
    "    }                                                                                           \n" \
    "}                                                                                               \n"

#define GAMMA_LUT_GLSL \
    "uniform sampler2D gamma_lut;                                                                    \n" \
    "uniform int gamma_lut_enabled;                                                                  \n" \
    "vec3 apply_gamma_lut(vec3 color) {                                                              \n" \
    "    int last_index = textureSize(gamma_lut, 0).x - 1;                                           \n" \
    "    vec3 pos = clamp(color, 0.0, 1.0) * float(last_index);                                      \n" \
    "    ivec3 index0 = ivec3(pos);                                                                  \n" \
    "    ivec3 index1 = min(index0 + 1, ivec3(last_index));                                          \n" \
    "    vec3 t = pos - vec3(index0);                                                                \n" \
    "    float r = mix(texelFetch(gamma_lut, ivec2(index0.r, 0), 0).r, texelFetch(gamma_lut, ivec2(index1.r, 0), 0).r, t.r); \n" \
    "    float g = mix(texelFetch(gamma_lut, ivec2(index0.g, 0), 0).g, texelFetch(gamma_lut, ivec2(index1.g, 0), 0).g, t.g); \n" \
    "    float b = mix(texelFetch(gamma_lut, ivec2(index0.b, 0), 0).b, texelFetch(gamma_lut, ivec2(index1.b, 0), 0).b, t.b); \n" \
    "    return vec3(r, g, b);                                                                       \n" \
    "}                                                                                               \n"

#define APPLY_HDR_TO_SDR_TONE_MAPPING \
    "  if(gamma_lut_enabled != 0)                                                    \n" \
    "    pixel.rgb = apply_gamma_lut(pixel.rgb);                                     \n" \
    "  if(color_matrix_transfer != 0)                                                \n" \
    "    pixel.rgb = apply_color_matrix(pixel.rgb);                                  \n" \
    "  if(hdr_sdr_white_scale > 0.0)                                                 \n" \
    "    pixel.rgb = tone_map_hdr_to_sdr(pixel.rgb);                                 \n"

static int destination_color_get_bit_depth(gsr_destination_color color_format) {
    switch(color_format) {
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P16:
        case GSR_DESTINATION_COLOR_XV30:
            return 10;
        default:
            return 8;
    }
}

static int destination_color_get_texture_bits(gsr_destination_color color_format) {
    switch(color_format) {
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P16:
            return 16;
        case GSR_DESTINATION_COLOR_XV30:
            return 10;
        default:
            return 8;
    }
}

static const char* destination_color_get_packed_yuv_swizzle(gsr_destination_color color_format) {
    switch(color_format) {
        case GSR_DESTINATION_COLOR_VUYX:
            return "zyx";
        case GSR_DESTINATION_COLOR_XV30:
            return "yxz";
        default:
            return NULL;
    }
}

/* ITU-R BT.709-6 item 3.4 and ITU-R BT.2020-2 table 5 for limited range, ITU-T H.273 for full range */
static void get_rgb_to_yuv_matrix(gsr_yuv_matrix yuv_matrix, gsr_color_range color_range, int bit_depth, int texture_bits, float *column_major_matrix) {
    const float kr = yuv_matrix == GSR_YUV_MATRIX_BT2020 ? 0.2627f : 0.2126f;
    const float kb = yuv_matrix == GSR_YUV_MATRIX_BT2020 ? 0.0593f : 0.0722f;
    const float kg = 1.0f - kr - kb;
    const float cb_scale = 0.5f / (1.0f - kb);
    const float cr_scale = 0.5f / (1.0f - kr);

    const float code_scale = (float)(1 << (bit_depth - 8));
    const float max_code = (float)((1 << bit_depth) - 1);
    const bool limited = color_range == GSR_COLOR_RANGE_LIMITED;
    const float luma_range = limited ? 219.0f * code_scale : max_code;
    const float luma_offset = limited ? 16.0f * code_scale : 0.0f;
    const float chroma_range = limited ? 224.0f * code_scale : max_code;
    const float chroma_offset = 128.0f * code_scale;
    const int unused_low_bits = texture_bits - bit_depth;
    /* Values are stored in the most significant bits of the texture and encoders discard the unused low bits, so half a code is added to round instead of truncate */
    const float rounding = unused_low_bits > 0 ? 0.5f : 0.0f;
    const float code_to_texture_value = (float)(1 << unused_low_bits) / (float)((1 << texture_bits) - 1);

    const float rows[3][4] = {
        { kr * luma_range,                 kg * luma_range,                 kb * luma_range,                 luma_offset + rounding   },
        { -kr * cb_scale * chroma_range,   -kg * cb_scale * chroma_range,   0.5f * chroma_range,             chroma_offset + rounding },
        { 0.5f * chroma_range,             -kg * cr_scale * chroma_range,   -kb * cr_scale * chroma_range,   chroma_offset + rounding },
    };

    for(int column = 0; column < 4; ++column) {
        for(int row = 0; row < 3; ++row) {
            column_major_matrix[column*4 + row] = rows[row][column] * code_to_texture_value;
        }
        column_major_matrix[column*4 + 3] = column == 3 ? 1.0f : 0.0f;
    }
}

static void rgb_to_yuv_matrix_to_glsl(const float *m, char *buffer, size_t buffer_size) {
    snprintf(buffer, buffer_size,
        "const mat4 RGBtoYUV = mat4(%.9f, %.9f, %.9f, %.9f,\n"
        "                           %.9f, %.9f, %.9f, %.9f,\n"
        "                           %.9f, %.9f, %.9f, %.9f,\n"
        "                           %.9f, %.9f, %.9f, %.9f);\n",
        m[0],  m[1],  m[2],  m[3],
        m[4],  m[5],  m[6],  m[7],
        m[8],  m[9],  m[10], m[11],
        m[12], m[13], m[14], m[15]);
}

typedef struct {
    const char *vertex_position;
    const char *fragment_outputs;
    const char *fragment_write;
} gsr_uv_shader_layout;

static bool destination_color_is_planar_yuv444(gsr_destination_color color_format) {
    return color_format == GSR_DESTINATION_COLOR_YUV444P || color_format == GSR_DESTINATION_COLOR_YUV444P16;
}

static bool destination_color_is_semi_planar_yuv444(gsr_destination_color color_format) {
    return color_format == GSR_DESTINATION_COLOR_NV24 || color_format == GSR_DESTINATION_COLOR_P410;
}

static gsr_uv_shader_layout get_uv_shader_layout(gsr_destination_color color_format) {
    const bool full_resolution_chroma = destination_color_is_planar_yuv444(color_format) || destination_color_is_semi_planar_yuv444(color_format);
    const char *vertex_position = full_resolution_chroma
        ? "  gl_Position = vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0);    \n"
        : "  gl_Position = (vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0)) * vec4(0.5, 0.5, 1.0, 1.0) - vec4(0.5, 0.5, 0.0, 0.0);   \n";

    if(destination_color_is_planar_yuv444(color_format)) {
        return (gsr_uv_shader_layout){
            .vertex_position = vertex_position,
            .fragment_outputs =
                "layout(location = 0) out vec4 FragColorU;                                             \n"
                "layout(location = 1) out vec4 FragColorV;                                             \n",
            .fragment_write =
                "  FragColorU = vec4(chroma.x, 0.0, 0.0, alpha);                                       \n"
                "  FragColorV = vec4(chroma.y, 0.0, 0.0, alpha);                                       \n",
        };
    }

    return (gsr_uv_shader_layout){
        .vertex_position = vertex_position,
        .fragment_outputs =
            "out vec4 FragColor;                                                                   \n",
        .fragment_write =
            "  FragColor.xy = chroma;                                                              \n"
            "  FragColor.w = alpha;                                                                \n",
    };
}

static int load_graphics_shader_y(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, const float *rgb_to_yuv_matrix, bool external_texture, bool hdr) {
    char color_transform_matrix[512];
    rgb_to_yuv_matrix_to_glsl(rgb_to_yuv_matrix, color_transform_matrix, sizeof(color_transform_matrix));

    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                   \n"
        "in vec2 pos;                                      \n"
        "in vec2 texcoords;                                \n"
        "out vec2 texcoords_out;                           \n"
        "uniform vec2 offset;                              \n"
        "uniform float rotation;                           \n"
        "uniform mat2 rotation_matrix;                     \n"
        "void main()                                       \n"
        "{                                                 \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);  \n"
        "  gl_Position = vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0);    \n"
        "}                                                 \n");

    char main_code[1024];
    snprintf(main_code, sizeof(main_code),
        "  vec4 pixel = texture(tex1, texcoords_out);                                    \n"
        "%s"
        "  FragColor.x = (RGBtoYUV * vec4(pixel.rgb, 1.0)).x;                            \n"
        "  FragColor.w = pixel.a;                                                        \n", hdr ? APPLY_HDR_TO_SDR_TONE_MAPPING : "");

    char fragment_shader[16384];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                 \n"
            "#extension GL_OES_EGL_image_external : enable                                   \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                            \n"
            "precision highp float;                                                        \n"
            "in vec2 texcoords_out;                                                          \n"
            "uniform samplerExternalOES tex1;                                                \n"
            "out vec4 FragColor;                                                             \n"
            "%s"
            "%s"
            "void main()                                                                     \n"
            "{                                                                               \n"
            "%s"
            "}                                                                               \n", color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                 \n"
            "precision highp float;                                                        \n"
            "in vec2 texcoords_out;                                                          \n"
            "uniform sampler2D tex1;                                                         \n"
            "out vec4 FragColor;                                                             \n"
            "%s"
            "%s"
            "void main()                                                                     \n"
            "{                                                                               \n"
            "%s"
            "}                                                                               \n", color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = egl->glGetUniformLocation(shader->program_id, "hdr_sdr_white_scale");
    uniforms->hdr_relative_peak_luminance = egl->glGetUniformLocation(shader->program_id, "hdr_relative_peak_luminance");
    uniforms->gamma_lut_enabled = egl->glGetUniformLocation(shader->program_id, "gamma_lut_enabled");
    uniforms->color_matrix_transfer = egl->glGetUniformLocation(shader->program_id, "color_matrix_transfer");
    uniforms->color_matrix = egl->glGetUniformLocation(shader->program_id, "color_matrix");

    egl->glUseProgram(shader->program_id);
    egl->glUniform1i(egl->glGetUniformLocation(shader->program_id, "gamma_lut"), 1);
    egl->glUseProgram(0);
    return 0;
}

static unsigned int load_graphics_shader_uv(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, const float *rgb_to_yuv_matrix, gsr_destination_color color_format, bool external_texture, bool hdr) {
    char color_transform_matrix[512];
    rgb_to_yuv_matrix_to_glsl(rgb_to_yuv_matrix, color_transform_matrix, sizeof(color_transform_matrix));
    const gsr_uv_shader_layout layout = get_uv_shader_layout(color_format);

    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                 \n"
        "in vec2 pos;                                    \n"
        "in vec2 texcoords;                              \n"
        "out vec2 texcoords_out;                         \n"
        "uniform vec2 offset;                            \n"
        "uniform float rotation;                         \n"
        "uniform mat2 rotation_matrix;                   \n"
        "void main()                                     \n"
        "{                                               \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);                      \n"
        "%s"
        "}                                               \n", layout.vertex_position);

    char main_code[2048];
    snprintf(main_code, sizeof(main_code),
            "  vec4 pixel = texture(tex1, texcoords_out);                                          \n"
            "%s"
            "  vec2 chroma = (RGBtoYUV * vec4(pixel.rgb, 1.0)).yz;                                 \n"
            "  float alpha = pixel.a;                                                              \n"
            "%s", hdr ? APPLY_HDR_TO_SDR_TONE_MAPPING : "", layout.fragment_write);

    char fragment_shader[16384];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "#extension GL_OES_EGL_image_external : enable                                         \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                                  \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform samplerExternalOES tex1;                                                      \n"
            "%s"
            "%s"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", layout.fragment_outputs, color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform sampler2D tex1;                                                               \n"
            "%s"
            "%s"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", layout.fragment_outputs, color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = egl->glGetUniformLocation(shader->program_id, "hdr_sdr_white_scale");
    uniforms->hdr_relative_peak_luminance = egl->glGetUniformLocation(shader->program_id, "hdr_relative_peak_luminance");
    uniforms->gamma_lut_enabled = egl->glGetUniformLocation(shader->program_id, "gamma_lut_enabled");
    uniforms->color_matrix_transfer = egl->glGetUniformLocation(shader->program_id, "color_matrix_transfer");
    uniforms->color_matrix = egl->glGetUniformLocation(shader->program_id, "color_matrix");

    egl->glUseProgram(shader->program_id);
    egl->glUniform1i(egl->glGetUniformLocation(shader->program_id, "gamma_lut"), 1);
    egl->glUseProgram(0);
    return 0;
}

static unsigned int load_graphics_shader_rgb(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, const float *rgb_to_yuv_matrix, gsr_destination_color color_format, bool external_texture, bool hdr) {
    const char *packed_yuv_swizzle = destination_color_get_packed_yuv_swizzle(color_format);
    char color_transform_matrix[512] = "";
    char output_code[256] = "  FragColor = pixel;\n";
    if(packed_yuv_swizzle) {
        rgb_to_yuv_matrix_to_glsl(rgb_to_yuv_matrix, color_transform_matrix, sizeof(color_transform_matrix));
        snprintf(output_code, sizeof(output_code), "  FragColor = vec4((RGBtoYUV * vec4(pixel.rgb, 1.0)).%s, pixel.a);\n", packed_yuv_swizzle);
    }
    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                   \n"
        "in vec2 pos;                                      \n"
        "in vec2 texcoords;                                \n"
        "out vec2 texcoords_out;                           \n"
        "uniform vec2 offset;                              \n"
        "uniform float rotation;                           \n"
        "uniform mat2 rotation_matrix;                     \n"
        "void main()                                       \n"
        "{                                                 \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);  \n"
        "  gl_Position = vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0);    \n"
        "}                                                 \n");

    char main_code[1024];
    snprintf(main_code, sizeof(main_code),
            "  vec4 pixel = texture(tex1, texcoords_out);                                          \n"
            "%s"
            "%s", hdr ? APPLY_HDR_TO_SDR_TONE_MAPPING : "", output_code);

    char fragment_shader[16384];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "#extension GL_OES_EGL_image_external : enable                                         \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                                  \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform samplerExternalOES tex1;                                                      \n"
            "out vec4 FragColor;                                                                   \n"
            "%s"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform sampler2D tex1;                                                               \n"
            "out vec4 FragColor;                                                                   \n"
            "%s"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", color_transform_matrix, hdr ? HDR_TO_SDR_TONE_MAPPING COLOR_MATRIX_GLSL GAMMA_LUT_GLSL : "", main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = egl->glGetUniformLocation(shader->program_id, "hdr_sdr_white_scale");
    uniforms->hdr_relative_peak_luminance = egl->glGetUniformLocation(shader->program_id, "hdr_relative_peak_luminance");
    uniforms->gamma_lut_enabled = egl->glGetUniformLocation(shader->program_id, "gamma_lut_enabled");
    uniforms->color_matrix_transfer = egl->glGetUniformLocation(shader->program_id, "color_matrix_transfer");
    uniforms->color_matrix = egl->glGetUniformLocation(shader->program_id, "color_matrix");

    egl->glUseProgram(shader->program_id);
    egl->glUniform1i(egl->glGetUniformLocation(shader->program_id, "gamma_lut"), 1);
    egl->glUseProgram(0);
    return 0;
}

static int load_graphics_shader_yuyv_to_y(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, bool external_texture) {
    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                   \n"
        "in vec2 pos;                                      \n"
        "in vec2 texcoords;                                \n"
        "out vec2 texcoords_out;                           \n"
        "uniform vec2 offset;                              \n"
        "uniform float rotation;                           \n"
        "uniform mat2 rotation_matrix;                     \n"
        "void main()                                       \n"
        "{                                                 \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);  \n"
        "  gl_Position = vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0);    \n"
        "}                                                 \n");

    const char *main_code =
            "  vec4 pixel = texture(tex1, texcoords_out);                                    \n"
            "  FragColor.x = pixel.r;                                                        \n"
            "  FragColor.w = 1.0;                                                            \n";

    char fragment_shader[2048];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                 \n"
            "#extension GL_OES_EGL_image_external : enable                                   \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                            \n"
            "precision highp float;                                                          \n"
            "in vec2 texcoords_out;                                                          \n"
            "uniform samplerExternalOES tex1;                                                \n"
            "out vec4 FragColor;                                                             \n"
            "void main()                                                                     \n"
            "{                                                                               \n"
            "%s"
            "}                                                                               \n", main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                 \n"
            "precision highp float;                                                          \n"
            "in vec2 texcoords_out;                                                          \n"
            "uniform sampler2D tex1;                                                         \n"
            "out vec4 FragColor;                                                             \n"
            "void main()                                                                     \n"
            "{                                                                               \n"
            "%s"
            "}                                                                               \n", main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = -1;
    uniforms->hdr_relative_peak_luminance = -1;
    uniforms->gamma_lut_enabled = -1;
    return 0;
}

static unsigned int load_graphics_shader_yuyv_to_uv(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, gsr_destination_color color_format, bool external_texture) {
    const gsr_uv_shader_layout layout = get_uv_shader_layout(color_format);

    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                 \n"
        "in vec2 pos;                                    \n"
        "in vec2 texcoords;                              \n"
        "out vec2 texcoords_out;                         \n"
        "uniform vec2 offset;                            \n"
        "uniform float rotation;                         \n"
        "uniform mat2 rotation_matrix;                   \n"
        "void main()                                     \n"
        "{                                               \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);                      \n"
        "%s"
        "}                                               \n", layout.vertex_position);

    char main_code[2048];
    snprintf(main_code, sizeof(main_code),
            "    vec2 resolution = vec2(textureSize(tex1, 0));\n"
            "    ivec2 uv = ivec2(texcoords_out * resolution);\n"
            "    float u = 0.0;\n"
            "    float v = 0.0;\n"
            "    vec4 this_color = texelFetch(tex1, uv, 0);\n"
            "    if((uv.x & 1) == 0) {\n"
            "        vec2 next_color = texelFetch(tex1, uv + ivec2(1, 0), 0).rg;\n"
            "        u = this_color.g;\n"
            "        v = next_color.g;\n"
            "    } else {\n"
            "        vec2 prev_color = texelFetch(tex1, uv - ivec2(1, 0), 0).rg;\n"
            "        u = prev_color.g;\n"
            "        v = this_color.g;\n"
            "    }\n"
            "    vec2 chroma = vec2(u, v);\n"
            "    float alpha = 1.0;\n"
            "%s", layout.fragment_write);

    char fragment_shader[4096];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "#extension GL_OES_EGL_image_external : enable                                         \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                                  \n"
            "precision highp float;                                                                \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform samplerExternalOES tex1;                                                      \n"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", layout.fragment_outputs, main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "precision highp float;                                                                \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform sampler2D tex1;                                                               \n"
            "%s"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", layout.fragment_outputs, main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = -1;
    uniforms->hdr_relative_peak_luminance = -1;
    uniforms->gamma_lut_enabled = -1;
    return 0;
}

static unsigned int load_graphics_shader_yuyv_to_rgb(gsr_shader *shader, gsr_egl *egl, gsr_color_graphics_uniforms *uniforms, gsr_destination_color color_format, bool external_texture) {
    const char *packed_yuv_swizzle = destination_color_get_packed_yuv_swizzle(color_format);
    char output_code[256] =
        "    FragColor = vec4(\n"
        "        y + 1.4065 * (v - 0.5),\n"
        "        y - 0.3455 * (u - 0.5) - 0.7169 * (v - 0.5),\n"
        "        y + 1.1790 * (u - 0.5),\n"
        "        1.0);\n";
    if(packed_yuv_swizzle)
        snprintf(output_code, sizeof(output_code), "    FragColor = vec4(vec3(y, u, v).%s, 1.0);\n", packed_yuv_swizzle);

    char vertex_shader[2048];
    snprintf(vertex_shader, sizeof(vertex_shader),
        "#version 300 es                                   \n"
        "in vec2 pos;                                      \n"
        "in vec2 texcoords;                                \n"
        "out vec2 texcoords_out;                           \n"
        "uniform vec2 offset;                              \n"
        "uniform float rotation;                           \n"
        "uniform mat2 rotation_matrix;                     \n"
        "void main()                                       \n"
        "{                                                 \n"
        "  texcoords_out = vec2(texcoords.x - 0.5, texcoords.y - 0.5) * rotation_matrix + vec2(0.5, 0.5);  \n"
        "  gl_Position = vec4(offset.x, offset.y, 0.0, 0.0) + vec4(pos.x, pos.y, 0.0, 1.0);    \n"
        "}                                                 \n");

    char main_code[2048];
    snprintf(main_code, sizeof(main_code),
        "    vec2 resolution = vec2(textureSize(tex1, 0));\n"
        "    ivec2 uv = ivec2(texcoords_out * resolution);\n"
        "    float y = 0.0;\n"
        "    float u = 0.0;\n"
        "    float v = 0.0;\n"
        "    vec4 this_color = texelFetch(tex1, uv, 0);\n"
        "    if((uv.x & 1) == 0) {\n"
        "        vec2 next_color = texelFetch(tex1, uv + ivec2(1, 0), 0).rg;\n"
        "        y = this_color.r;\n"
        "        u = this_color.g;\n"
        "        v = next_color.g;\n"
        "    } else {\n"
        "        vec2 prev_color = texelFetch(tex1, uv - ivec2(1, 0), 0).rg;\n"
        "        y = this_color.r;\n"
        "        u = prev_color.g;\n"
        "        v = this_color.g;\n"
        "    }\n"
        "%s", output_code);

    char fragment_shader[4096];
    if(external_texture) {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "#extension GL_OES_EGL_image_external : enable                                         \n"
            "#extension GL_OES_EGL_image_external_essl3 : require                                  \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform samplerExternalOES tex1;                                                      \n"
            "out vec4 FragColor;                                                                   \n"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", main_code);
    } else {
        snprintf(fragment_shader, sizeof(fragment_shader),
            "#version 300 es                                                                       \n"
            "precision highp float;                                                              \n"
            "in vec2 texcoords_out;                                                                \n"
            "uniform sampler2D tex1;                                                               \n"
            "out vec4 FragColor;                                                                   \n"
            "void main()                                                                           \n"
            "{                                                                                     \n"
            "%s"
            "}                                                                                     \n", main_code);
    }

    if(gsr_shader_init(shader, egl, vertex_shader, fragment_shader) != 0)
        return -1;

    gsr_shader_bind_attribute_location(shader, "pos", 0);
    gsr_shader_bind_attribute_location(shader, "texcoords", 1);
    uniforms->offset = egl->glGetUniformLocation(shader->program_id, "offset");
    uniforms->rotation_matrix = egl->glGetUniformLocation(shader->program_id, "rotation_matrix");
    uniforms->hdr_sdr_white_scale = -1;
    uniforms->hdr_relative_peak_luminance = -1;
    uniforms->gamma_lut_enabled = -1;
    return 0;
}

static bool gsr_color_conversion_load_hdr_graphics_shaders(gsr_color_conversion *self) {
    switch(self->params.destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_NV24:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P:
        case GSR_DESTINATION_COLOR_YUV444P16: {
            if(load_graphics_shader_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_Y_HDR], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_Y_HDR], self->rgb_to_yuv_matrix, false, true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load Y graphics shader (hdr)");
                return false;
            }

            if(load_graphics_shader_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_UV_HDR], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_UV_HDR], self->rgb_to_yuv_matrix, self->params.destination_color, false, true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load UV graphics shader (hdr)");
                return false;
            }

            if(self->params.load_external_image_shader) {
                if(load_graphics_shader_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_Y_HDR_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_Y_HDR_EXTERNAL], self->rgb_to_yuv_matrix, true, true) != 0) {
                    gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load Y graphics shader (hdr, external)");
                    return false;
                }

                if(load_graphics_shader_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_UV_HDR_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_UV_HDR_EXTERNAL], self->rgb_to_yuv_matrix, self->params.destination_color, true, true) != 0) {
                    gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load UV graphics shader (hdr, external)");
                    return false;
                }
            }
            break;
        }
        case GSR_DESTINATION_COLOR_VUYX:
        case GSR_DESTINATION_COLOR_XV30:
        case GSR_DESTINATION_COLOR_RGB: {
            if(load_graphics_shader_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_RGB_HDR], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_RGB_HDR], self->rgb_to_yuv_matrix, self->params.destination_color, false, true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load RGB graphics shader (hdr)");
                return false;
            }

            if(self->params.load_external_image_shader) {
                if(load_graphics_shader_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_RGB_HDR_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_RGB_HDR_EXTERNAL], self->rgb_to_yuv_matrix, self->params.destination_color, true, true) != 0) {
                    gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_load_hdr_graphics_shaders: failed to load RGB graphics shader (hdr, external)");
                    return false;
                }
            }
            break;
        }
    }
    return true;
}

static bool gsr_color_conversion_load_hdr_graphics_shaders_if_needed(gsr_color_conversion *self) {
    const bool hdr_enabled = self->hdr_sdr_white_scale > 0.0f || (self->gamma_lut_apply && self->gamma_lut_texture_id != 0) || self->color_matrix_transfer != 0;
    if(!hdr_enabled || self->hdr_shaders_load_failed)
        return false;

    if(self->hdr_shaders_loaded)
        return true;

    if(!gsr_color_conversion_load_hdr_graphics_shaders(self)) {
        self->hdr_shaders_load_failed = true;
        return false;
    }

    self->hdr_shaders_loaded = true;
    return true;
}

static int attach_destination_textures(gsr_color_conversion *self) {
    const unsigned int draw_buffer = GL_COLOR_ATTACHMENT0;
    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
    self->params.egl->glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, self->params.destination_textures[0], 0);
    self->params.egl->glDrawBuffers(1, &draw_buffer);
    if(self->params.egl->glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to create framebuffer for Y");
        goto err;
    }

    if(self->params.num_destination_textures > 1) {
        const unsigned int chroma_draw_buffers[2] = { GL_COLOR_ATTACHMENT0, GL_COLOR_ATTACHMENT1 };
        const int num_chroma_textures = self->params.num_destination_textures - 1;
        self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[1]);
        for(int i = 0; i < num_chroma_textures; ++i) {
            self->params.egl->glFramebufferTexture2D(GL_FRAMEBUFFER, chroma_draw_buffers[i], GL_TEXTURE_2D, self->params.destination_textures[1 + i], 0);
        }
        self->params.egl->glDrawBuffers(num_chroma_textures, chroma_draw_buffers);
        if(self->params.egl->glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to create framebuffer for UV");
            goto err;
        }
    }

    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
    return 0;

    err:
    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
    return -1;
}

static int load_framebuffers(gsr_color_conversion *self) {
    /* TODO: Only generate the necessary amount of framebuffers (self->params.num_destination_textures) */
    self->params.egl->glGenFramebuffers(GSR_COLOR_CONVERSION_MAX_FRAMEBUFFERS, self->framebuffers);
    return attach_destination_textures(self);
}

static int create_vertices(gsr_color_conversion *self) {
    self->params.egl->glGenVertexArrays(1, &self->vertex_array_object_id);
    self->params.egl->glBindVertexArray(self->vertex_array_object_id);

    self->params.egl->glGenBuffers(1, &self->vertex_buffer_object_id);
    self->params.egl->glBindBuffer(GL_ARRAY_BUFFER, self->vertex_buffer_object_id);
    self->params.egl->glBufferData(GL_ARRAY_BUFFER, 24 * sizeof(float), NULL, GL_DYNAMIC_DRAW);

    self->params.egl->glEnableVertexAttribArray(0);
    self->params.egl->glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void*)0);

    self->params.egl->glEnableVertexAttribArray(1);
    self->params.egl->glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void*)(2 * sizeof(float)));

    self->params.egl->glBindVertexArray(0);
    return 0;
}

static bool gsr_color_conversion_load_graphics_shaders(gsr_color_conversion *self) {
    switch(self->params.destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_NV24:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P:
        case GSR_DESTINATION_COLOR_YUV444P16: {
            if(load_graphics_shader_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_Y], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_Y], self->rgb_to_yuv_matrix, false, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load Y graphics shader");
                return false;
            }

            if(load_graphics_shader_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_UV], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_UV], self->rgb_to_yuv_matrix, self->params.destination_color, false, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load UV graphics shader");
                return false;
            }

            if(load_graphics_shader_yuyv_to_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_Y], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_Y], false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to Y graphics shader");
                return false;
            }

            if(load_graphics_shader_yuyv_to_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_UV], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_UV], self->params.destination_color, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to UV graphics shader");
                return false;
            }
            break;
        }
        case GSR_DESTINATION_COLOR_VUYX:
        case GSR_DESTINATION_COLOR_XV30:
        case GSR_DESTINATION_COLOR_RGB: {
            if(load_graphics_shader_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_RGB], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_RGB], self->rgb_to_yuv_matrix, self->params.destination_color, false, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load RGB graphics shader");
                return false;
            }

            if(load_graphics_shader_yuyv_to_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_RGB], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_RGB], self->params.destination_color, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to RGB graphics shader");
                return false;
            }
            break;
        }
    }
    return true;
}

static bool gsr_color_conversion_load_external_graphics_shaders(gsr_color_conversion *self) {
    switch(self->params.destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_NV24:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P:
        case GSR_DESTINATION_COLOR_YUV444P16: {
            if(load_graphics_shader_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_Y_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_Y_EXTERNAL], self->rgb_to_yuv_matrix, true, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load Y graphics shader (external)");
                return false;
            }

            if(load_graphics_shader_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_UV_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_UV_EXTERNAL], self->rgb_to_yuv_matrix, self->params.destination_color, true, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load UV graphics shader (external)");
                return false;
            }

            if(load_graphics_shader_yuyv_to_y(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_Y_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_Y_EXTERNAL], true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to Y graphics shader (external)");
                return false;
            }

            if(load_graphics_shader_yuyv_to_uv(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_UV_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_UV_EXTERNAL], self->params.destination_color, true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to UV graphics shader (external)");
                return false;
            }
            break;
        }
        case GSR_DESTINATION_COLOR_VUYX:
        case GSR_DESTINATION_COLOR_XV30:
        case GSR_DESTINATION_COLOR_RGB: {
            if(load_graphics_shader_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_RGB_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_RGB_EXTERNAL], self->rgb_to_yuv_matrix, self->params.destination_color, true, false) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load RGB graphics shader (external)");
                return false;
            }

            if(load_graphics_shader_yuyv_to_rgb(&self->graphics_shaders[GRAPHICS_SHADER_INDEX_YUYV_TO_RGB_EXTERNAL], self->params.egl, &self->graphics_uniforms[GRAPHICS_SHADER_INDEX_YUYV_TO_RGB_EXTERNAL], self->params.destination_color, true) != 0) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: failed to load YUYV to RGB graphics shader (external)");
                return false;
            }
            break;
        }
    }
    return true;
}

int gsr_color_conversion_init(gsr_color_conversion *self, const gsr_color_conversion_params *params) {
    assert(params);
    assert(params->egl);
    memset(self, 0, sizeof(*self));
    self->params.egl = params->egl;
    self->params = *params;
    self->clear_generation = 1;

    switch(self->params.destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_NV24:
        case GSR_DESTINATION_COLOR_P410: {
            if(self->params.num_destination_textures != 2) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: expected 2 destination textures for destination color NV12/P010/NV24/P410, got %d destination texture(s)", self->params.num_destination_textures);
                goto err;
            }
            break;
        }
        case GSR_DESTINATION_COLOR_YUV444P:
        case GSR_DESTINATION_COLOR_YUV444P16: {
            if(self->params.num_destination_textures != 3) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: expected 3 destination textures for destination color YUV444P/YUV444P16, got %d destination texture(s)", self->params.num_destination_textures);
                goto err;
            }
            break;
        }
        case GSR_DESTINATION_COLOR_VUYX:
        case GSR_DESTINATION_COLOR_XV30:
        case GSR_DESTINATION_COLOR_RGB: {
            if(self->params.num_destination_textures != 1) {
                gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_init: expected 1 destination textures for destination color RGB/VUYX/XV30, got %d destination texture(s)", self->params.num_destination_textures);
                goto err;
            }
            break;
        }
    }

    get_rgb_to_yuv_matrix(self->params.yuv_matrix, self->params.color_range, destination_color_get_bit_depth(self->params.destination_color), destination_color_get_texture_bits(self->params.destination_color), self->rgb_to_yuv_matrix);

    if(!gsr_color_conversion_load_graphics_shaders(self))
        goto err;

    if(self->params.load_external_image_shader) {
        if(!gsr_color_conversion_load_external_graphics_shaders(self))
            goto err;
    }

    if(load_framebuffers(self) != 0)
        goto err;

    if(create_vertices(self) != 0)
        goto err;

    return 0;

    err:
    gsr_color_conversion_deinit(self);
    return -1;
}

void gsr_color_conversion_deinit(gsr_color_conversion *self) {
    if(!self->params.egl)
        return;

    if(self->gamma_lut_texture_id) {
        self->params.egl->glDeleteTextures(1, &self->gamma_lut_texture_id);
        self->gamma_lut_texture_id = 0;
    }

    if(self->vertex_buffer_object_id) {
        self->params.egl->glDeleteBuffers(1, &self->vertex_buffer_object_id);
        self->vertex_buffer_object_id = 0;
    }

    if(self->vertex_array_object_id) {
        self->params.egl->glDeleteVertexArrays(1, &self->vertex_array_object_id);
        self->vertex_array_object_id = 0;
    }

    self->params.egl->glDeleteFramebuffers(GSR_COLOR_CONVERSION_MAX_FRAMEBUFFERS, self->framebuffers);
    for(int i = 0; i < GSR_COLOR_CONVERSION_MAX_FRAMEBUFFERS; ++i) {
        self->framebuffers[i] = 0;
    }

    for(int i = 0; i < GSR_COLOR_CONVERSION_MAX_GRAPHICS_SHADERS; ++i) {
        gsr_shader_deinit(&self->graphics_shaders[i]);
    }

    self->params.egl = NULL;
}

static void gsr_color_conversion_apply_rotation(gsr_rotation rotation, float rotation_matrix[2][2]) {
    /*
    rotation_matrix[0][0] =  cos(angle);
    rotation_matrix[0][1] = -sin(angle);
    rotation_matrix[1][0] =  sin(angle);
    rotation_matrix[1][1] =  cos(angle);
    The manual matrix code below is the same as this code above, but without floating-point errors.
    This is done to remove any blurring caused by these floating-point errors.
    */
    switch(rotation) {
        case GSR_ROT_0:
            rotation_matrix[0][0] = 1.0f;
            rotation_matrix[0][1] = 0.0f;
            rotation_matrix[1][0] = 0.0f;
            rotation_matrix[1][1] = 1.0f;
            break;
        case GSR_ROT_90:
            rotation_matrix[0][0] =  0.0f;
            rotation_matrix[0][1] = -1.0f;
            rotation_matrix[1][0] =  1.0f;
            rotation_matrix[1][1] =  0.0f;
            break;
        case GSR_ROT_180:
            rotation_matrix[0][0] = -1.0f;
            rotation_matrix[0][1] =  0.0f;
            rotation_matrix[1][0] =  0.0f;
            rotation_matrix[1][1] = -1.0f;
            break;
        case GSR_ROT_270:
            rotation_matrix[0][0] =  0.0f;
            rotation_matrix[0][1] =  1.0f;
            rotation_matrix[1][0] = -1.0f;
            rotation_matrix[1][1] =  0.0f;
            break;
    }
}

static void gsr_color_conversion_swizzle_texture_source(gsr_color_conversion *self, unsigned int texture_target, gsr_source_color source_color) {
    if(source_color == GSR_SOURCE_COLOR_BGR) {
        const int swizzle_mask[] = { GL_BLUE, GL_GREEN, GL_RED, 1 };
        self->params.egl->glTexParameteriv(texture_target, GL_TEXTURE_SWIZZLE_RGBA, swizzle_mask);
    }
}

static void gsr_color_conversion_swizzle_reset(gsr_color_conversion *self, unsigned int texture_target, gsr_source_color source_color) {
    if(source_color == GSR_SOURCE_COLOR_BGR) {
        const int swizzle_mask[] = { GL_RED, GL_GREEN, GL_BLUE, GL_ALPHA };
        self->params.egl->glTexParameteriv(texture_target, GL_TEXTURE_SWIZZLE_RGBA, swizzle_mask);
    }
}

static void gsr_color_conversion_draw_graphics(gsr_color_conversion *self, unsigned int texture_id, bool external_texture, gsr_rotation rotation, gsr_flip flip, float rotation_matrix[2][2], vec2i source_position, vec2i source_size, vec2i destination_pos, vec2i texture_size, vec2f scale, gsr_source_color source_color) {
    if(source_size.x == 0 || source_size.y == 0)
        return;

    const vec2i dest_texture_size = self->params.destination_textures_size[0];
    const unsigned int texture_target = external_texture ? GL_TEXTURE_EXTERNAL_OES : GL_TEXTURE_2D;

    if(rotation == GSR_ROT_90 || rotation == GSR_ROT_270) {
        const float tmp = texture_size.x;
        texture_size.x = texture_size.y;
        texture_size.y = tmp;
    }

    self->params.egl->glBindTexture(texture_target, texture_id);
    gsr_color_conversion_swizzle_texture_source(self, texture_target, source_color);

    const bool use_hdr_shaders = gsr_color_conversion_load_hdr_graphics_shaders_if_needed(self);
    const int gamma_lut_enabled = (use_hdr_shaders && self->gamma_lut_apply && self->gamma_lut_texture_id) ? 1 : 0;
    if(gamma_lut_enabled) {
        self->params.egl->glActiveTexture(GL_TEXTURE1);
        self->params.egl->glBindTexture(GL_TEXTURE_2D, self->gamma_lut_texture_id);
        self->params.egl->glActiveTexture(GL_TEXTURE0);
    }

    const vec2f pos_norm = {
        ((float)destination_pos.x / (dest_texture_size.x == 0 ? 1.0f : (float)dest_texture_size.x)) * 2.0f,
        ((float)destination_pos.y / (dest_texture_size.y == 0 ? 1.0f : (float)dest_texture_size.y)) * 2.0f,
    };

    const vec2f size_norm = {
        ((float)source_size.x / (dest_texture_size.x == 0 ? 1.0f : (float)dest_texture_size.x)) * 2.0f * scale.x,
        ((float)source_size.y / (dest_texture_size.y == 0 ? 1.0f : (float)dest_texture_size.y)) * 2.0f * scale.y,
    };

    const vec2f texture_pos_norm = {
        (float)source_position.x / (texture_size.x == 0 ? 1.0f : (float)texture_size.x),
        (float)source_position.y / (texture_size.y == 0 ? 1.0f : (float)texture_size.y),
    };

    const vec2f texture_size_norm = {
        (float)source_size.x / (texture_size.x == 0 ? 1.0f : (float)texture_size.x),
        (float)source_size.y / (texture_size.y == 0 ? 1.0f : (float)texture_size.y),
    };

    float vertices[] = {
        -1.0f + 0.0f,               -1.0f + 0.0f + size_norm.y, texture_pos_norm.x,                       texture_pos_norm.y + texture_size_norm.y,
        -1.0f + 0.0f,               -1.0f + 0.0f,               texture_pos_norm.x,                       texture_pos_norm.y,
        -1.0f + 0.0f + size_norm.x, -1.0f + 0.0f,               texture_pos_norm.x + texture_size_norm.x, texture_pos_norm.y,

        -1.0f + 0.0f,               -1.0f + 0.0f + size_norm.y, texture_pos_norm.x,                       texture_pos_norm.y + texture_size_norm.y,
        -1.0f + 0.0f + size_norm.x, -1.0f + 0.0f,               texture_pos_norm.x + texture_size_norm.x, texture_pos_norm.y,
        -1.0f + 0.0f + size_norm.x, -1.0f + 0.0f + size_norm.y, texture_pos_norm.x + texture_size_norm.x, texture_pos_norm.y + texture_size_norm.y
    };

    if(flip & GSR_FLIP_HORIZONTAL) {
        for(int i = 0; i < 6; ++i) {
            const float prev_x = vertices[i*4 + 2];
            vertices[i*4 + 2] = texture_pos_norm.x + texture_size_norm.x - prev_x;
        }
    }

    if(flip & GSR_FLIP_VERTICAL) {
        for(int i = 0; i < 6; ++i) {
            const float prev_y = vertices[i*4 + 3];
            vertices[i*4 + 3] = texture_pos_norm.y + texture_size_norm.y - prev_y;
        }
    }

    self->params.egl->glBindVertexArray(self->vertex_array_object_id);
    self->params.egl->glViewport(0, 0, dest_texture_size.x, dest_texture_size.y);

    /* TODO: this, also cleanup */
    self->params.egl->glBindBuffer(GL_ARRAY_BUFFER, self->vertex_buffer_object_id);
    self->params.egl->glBufferSubData(GL_ARRAY_BUFFER, 0, 24 * sizeof(float), vertices);

    switch(source_color) {
        case GSR_SOURCE_COLOR_RGB:
        case GSR_SOURCE_COLOR_BGR: {
            switch(self->params.destination_color) {
                case GSR_DESTINATION_COLOR_NV12:
                case GSR_DESTINATION_COLOR_P010:
                case GSR_DESTINATION_COLOR_NV24:
                case GSR_DESTINATION_COLOR_P410:
                case GSR_DESTINATION_COLOR_YUV444P:
                case GSR_DESTINATION_COLOR_YUV444P16: {
                    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
                    //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT); // TODO: Do this in a separate clear_ function. We want to do that when using multiple drm to create the final image (multiple monitors for example)

                    int shader_index;
                    if(use_hdr_shaders)
                        shader_index = external_texture ? GRAPHICS_SHADER_INDEX_Y_HDR_EXTERNAL : GRAPHICS_SHADER_INDEX_Y_HDR;
                    else
                        shader_index = external_texture ? GRAPHICS_SHADER_INDEX_Y_EXTERNAL : GRAPHICS_SHADER_INDEX_Y;
                    gsr_shader_use(&self->graphics_shaders[shader_index]);
                    self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                    self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                    if(use_hdr_shaders) {
                        self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_sdr_white_scale, self->hdr_sdr_white_scale);
                        self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_relative_peak_luminance, self->hdr_relative_peak_luminance);
                        self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].gamma_lut_enabled, gamma_lut_enabled);
                        self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].color_matrix_transfer, self->color_matrix_transfer);
                        self->params.egl->glUniformMatrix3fv(self->graphics_uniforms[shader_index].color_matrix, 1, GL_TRUE, self->color_matrix);
                    }
                    self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);

                    if(self->params.num_destination_textures > 1) {
                        self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[1]);
                        //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT);

                        if(use_hdr_shaders)
                            shader_index = external_texture ? GRAPHICS_SHADER_INDEX_UV_HDR_EXTERNAL : GRAPHICS_SHADER_INDEX_UV_HDR;
                        else
                            shader_index = external_texture ? GRAPHICS_SHADER_INDEX_UV_EXTERNAL : GRAPHICS_SHADER_INDEX_UV;
                        gsr_shader_use(&self->graphics_shaders[shader_index]);
                        self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                        self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                        if(use_hdr_shaders) {
                            self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_sdr_white_scale, self->hdr_sdr_white_scale);
                            self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_relative_peak_luminance, self->hdr_relative_peak_luminance);
                            self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].gamma_lut_enabled, gamma_lut_enabled);
                            self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].color_matrix_transfer, self->color_matrix_transfer);
                            self->params.egl->glUniformMatrix3fv(self->graphics_uniforms[shader_index].color_matrix, 1, GL_TRUE, self->color_matrix);
                        }
                        self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);
                    }
                    break;
                }
                case GSR_DESTINATION_COLOR_VUYX:
                case GSR_DESTINATION_COLOR_XV30:
                case GSR_DESTINATION_COLOR_RGB: {
                    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
                    //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT); // TODO: Do this in a separate clear_ function. We want to do that when using multiple drm to create the final image (multiple monitors for example)

                    int shader_index;
                    if(use_hdr_shaders)
                        shader_index = external_texture ? GRAPHICS_SHADER_INDEX_RGB_HDR_EXTERNAL : GRAPHICS_SHADER_INDEX_RGB_HDR;
                    else
                        shader_index = external_texture ? GRAPHICS_SHADER_INDEX_RGB_EXTERNAL : GRAPHICS_SHADER_INDEX_RGB;
                    gsr_shader_use(&self->graphics_shaders[shader_index]);
                    self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                    self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                    if(use_hdr_shaders) {
                        self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_sdr_white_scale, self->hdr_sdr_white_scale);
                        self->params.egl->glUniform1f(self->graphics_uniforms[shader_index].hdr_relative_peak_luminance, self->hdr_relative_peak_luminance);
                        self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].gamma_lut_enabled, gamma_lut_enabled);
                        self->params.egl->glUniform1i(self->graphics_uniforms[shader_index].color_matrix_transfer, self->color_matrix_transfer);
                        self->params.egl->glUniformMatrix3fv(self->graphics_uniforms[shader_index].color_matrix, 1, GL_TRUE, self->color_matrix);
                    }
                    self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);
                    break;
                }
            }
            break;
        }
        case GSR_SOURCE_COLOR_YUYV: {
            switch(self->params.destination_color) {
                case GSR_DESTINATION_COLOR_NV12:
                case GSR_DESTINATION_COLOR_P010:
                case GSR_DESTINATION_COLOR_NV24:
                case GSR_DESTINATION_COLOR_P410:
                case GSR_DESTINATION_COLOR_YUV444P:
                case GSR_DESTINATION_COLOR_YUV444P16: {
                    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
                    //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT); // TODO: Do this in a separate clear_ function. We want to do that when using multiple drm to create the final image (multiple monitors for example)

                    int shader_index = external_texture ? GRAPHICS_SHADER_INDEX_YUYV_TO_Y_EXTERNAL : GRAPHICS_SHADER_INDEX_YUYV_TO_Y;
                    gsr_shader_use(&self->graphics_shaders[shader_index]);
                    self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                    self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                    self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);

                    if(self->params.num_destination_textures > 1) {
                        self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[1]);
                        //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT);

                        shader_index = external_texture ? GRAPHICS_SHADER_INDEX_YUYV_TO_UV_EXTERNAL : GRAPHICS_SHADER_INDEX_YUYV_TO_UV;
                        gsr_shader_use(&self->graphics_shaders[shader_index]);
                        self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                        self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                        self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);
                    }
                    break;
                }
                case GSR_DESTINATION_COLOR_VUYX:
                case GSR_DESTINATION_COLOR_XV30:
                case GSR_DESTINATION_COLOR_RGB: {
                    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
                    //cap_xcomp->params.egl->glClear(GL_COLOR_BUFFER_BIT); // TODO: Do this in a separate clear_ function. We want to do that when using multiple drm to create the final image (multiple monitors for example)

                    const int shader_index = external_texture ? GRAPHICS_SHADER_INDEX_YUYV_TO_RGB_EXTERNAL : GRAPHICS_SHADER_INDEX_YUYV_TO_RGB;
                    gsr_shader_use(&self->graphics_shaders[shader_index]);
                    self->params.egl->glUniformMatrix2fv(self->graphics_uniforms[shader_index].rotation_matrix, 1, GL_TRUE, (const float*)rotation_matrix);
                    self->params.egl->glUniform2f(self->graphics_uniforms[shader_index].offset, pos_norm.x, pos_norm.y);
                    self->params.egl->glDrawArrays(GL_TRIANGLES, 0, 6);
                    break;
                }
            }
            break;
        }
    }

    self->params.egl->glBindVertexArray(0);
    self->params.egl->glUseProgram(0);
    gsr_color_conversion_swizzle_reset(self, texture_target, source_color);
    if(gamma_lut_enabled) {
        self->params.egl->glActiveTexture(GL_TEXTURE1);
        self->params.egl->glBindTexture(GL_TEXTURE_2D, 0);
        self->params.egl->glActiveTexture(GL_TEXTURE0);
    }
    self->params.egl->glBindTexture(texture_target, 0);
    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void gsr_color_conversion_draw(gsr_color_conversion *self, unsigned int texture_id, vec2i destination_pos, vec2i destination_size, vec2i source_pos, vec2i source_size, vec2i texture_size, gsr_rotation rotation, gsr_flip flip, gsr_source_color source_color, bool external_texture) {
    assert(!external_texture || self->params.load_external_image_shader);
    if(external_texture && !self->params.load_external_image_shader) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_color_conversion_draw: external texture not loaded");
        return;
    }

    vec2f scale = {0.0f, 0.0f};
    if(source_size.x > 0 && source_size.y > 0)
        scale = (vec2f){ (double)destination_size.x/(double)source_size.x, (double)destination_size.y/(double)source_size.y };

    vec2i source_position = {0, 0};
    float rotation_matrix[2][2] = {{0, 0}, {0, 0}};
    gsr_color_conversion_apply_rotation(rotation, rotation_matrix);

    const int texture_target = external_texture ? GL_TEXTURE_EXTERNAL_OES : GL_TEXTURE_2D;
    self->params.egl->glBindTexture(texture_target, texture_id);

    source_position.x += source_pos.x;
    source_position.y += source_pos.y;
    gsr_color_conversion_draw_graphics(self, texture_id, external_texture, rotation, flip, rotation_matrix, source_position, source_size, destination_pos, texture_size, scale, source_color);

    self->params.egl->glFlush();
    // TODO: Use the minimal barrier required
    self->params.egl->glMemoryBarrier(GL_ALL_BARRIER_BITS); // GL_SHADER_IMAGE_ACCESS_BARRIER_BIT
    self->params.egl->glUseProgram(0);

    self->params.egl->glBindTexture(texture_target, 0);
}

static void clear_destination_textures(gsr_color_conversion *self) {
    float color1[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    float color2[4] = {0.0f, 0.0f, 0.0f, 1.0f};

    switch(self->params.destination_color) {
        case GSR_DESTINATION_COLOR_NV12:
        case GSR_DESTINATION_COLOR_P010:
        case GSR_DESTINATION_COLOR_NV24:
        case GSR_DESTINATION_COLOR_P410:
        case GSR_DESTINATION_COLOR_YUV444P:
        case GSR_DESTINATION_COLOR_YUV444P16: {
            color1[0] = self->rgb_to_yuv_matrix[12];
            color2[0] = self->rgb_to_yuv_matrix[13];
            color2[1] = self->rgb_to_yuv_matrix[14];
            break;
        }
        case GSR_DESTINATION_COLOR_VUYX: {
            color1[0] = self->rgb_to_yuv_matrix[14];
            color1[1] = self->rgb_to_yuv_matrix[13];
            color1[2] = self->rgb_to_yuv_matrix[12];
            break;
        }
        case GSR_DESTINATION_COLOR_XV30: {
            color1[0] = self->rgb_to_yuv_matrix[13];
            color1[1] = self->rgb_to_yuv_matrix[12];
            color1[2] = self->rgb_to_yuv_matrix[14];
            break;
        }
        case GSR_DESTINATION_COLOR_RGB: {
            break;
        }
    }

    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[0]);
    self->params.egl->glClearColor(color1[0], color1[1], color1[2], color1[3]);
    self->params.egl->glClear(GL_COLOR_BUFFER_BIT);

    if(self->params.num_destination_textures > 1) {
        self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[1]);
        self->params.egl->glClearColor(color2[0], color2[1], color2[2], color2[3]);
        self->params.egl->glClear(GL_COLOR_BUFFER_BIT);
    }

    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void gsr_color_conversion_clear(gsr_color_conversion *self) {
    clear_destination_textures(self);
    ++self->clear_generation;
}

void gsr_color_conversion_set_destination_textures(gsr_color_conversion *self, const unsigned int *destination_textures, uint64_t *destination_clear_generation) {
    memcpy(self->params.destination_textures, destination_textures, self->params.num_destination_textures * sizeof(*destination_textures));
    attach_destination_textures(self);

    if(*destination_clear_generation != self->clear_generation) {
        clear_destination_textures(self);
        *destination_clear_generation = self->clear_generation;
    }
}

void gsr_color_conversion_set_hdr_to_sdr_tone_mapping(gsr_color_conversion *self, bool enable, float hdr_peak_luminance, float sdr_white_luminance) {
    if(!enable) {
        self->hdr_sdr_white_scale = 0.0f;
        return;
    }

    if(sdr_white_luminance <= 0.0f)
        sdr_white_luminance = SDR_WHITE_LUMINANCE_DEFAULT;

    if(sdr_white_luminance < 80.0f)
        sdr_white_luminance = 80.0f;
    else if(sdr_white_luminance > 10000.0f)
        sdr_white_luminance = 10000.0f;

    if(hdr_peak_luminance < sdr_white_luminance)
        hdr_peak_luminance = sdr_white_luminance;
    else if(hdr_peak_luminance > 10000.0f)
        hdr_peak_luminance = 10000.0f;

    self->hdr_sdr_white_scale = 10000.0f / sdr_white_luminance;
    self->hdr_relative_peak_luminance = hdr_peak_luminance / sdr_white_luminance;
}

void gsr_color_conversion_set_gamma_lut(gsr_color_conversion *self, const float *rgb_values, int num_entries) {
    if(!rgb_values || num_entries <= 0) {
        if(self->gamma_lut_texture_id) {
            self->params.egl->glDeleteTextures(1, &self->gamma_lut_texture_id);
            self->gamma_lut_texture_id = 0;
        }
        return;
    }

    if(self->gamma_lut_texture_id == 0) {
        self->params.egl->glGenTextures(1, &self->gamma_lut_texture_id);
        self->params.egl->glBindTexture(GL_TEXTURE_2D, self->gamma_lut_texture_id);
        self->params.egl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        self->params.egl->glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    } else {
        self->params.egl->glBindTexture(GL_TEXTURE_2D, self->gamma_lut_texture_id);
    }

    self->params.egl->glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB32F, num_entries, 1, 0, GL_RGB, GL_FLOAT, rgb_values);
    self->params.egl->glBindTexture(GL_TEXTURE_2D, 0);
}

void gsr_color_conversion_enable_gamma_lut(gsr_color_conversion *self, bool enable) {
    self->gamma_lut_apply = enable;
}

void gsr_color_conversion_set_color_matrix(gsr_color_conversion *self, const float *linear_rgb_matrix, gsr_color_matrix_transfer transfer) {
    if(!linear_rgb_matrix) {
        self->color_matrix_transfer = 0;
        return;
    }

    memcpy(self->color_matrix, linear_rgb_matrix, sizeof(self->color_matrix));
    self->color_matrix_transfer = transfer;
}

void gsr_color_conversion_read_destination_texture(gsr_color_conversion *self, int destination_texture_index, int x, int y, int width, int height, unsigned int color_format, unsigned int data_format, void *pixels) {
    assert(destination_texture_index >= 0 && destination_texture_index < self->params.num_destination_textures);
    const int framebuffer_index = destination_texture_index == 0 ? 0 : 1;
    const int attachment_index = destination_texture_index - framebuffer_index;
    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, self->framebuffers[framebuffer_index]);
    self->params.egl->glReadBuffer(GL_COLOR_ATTACHMENT0 + attachment_index);
    self->params.egl->glReadPixels(x, y, width, height, color_format, data_format, pixels);
    self->params.egl->glReadBuffer(GL_COLOR_ATTACHMENT0);
    self->params.egl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

gsr_rotation gsr_monitor_rotation_to_rotation(gsr_monitor_rotation monitor_rotation) {
    return (gsr_rotation)monitor_rotation;
}
