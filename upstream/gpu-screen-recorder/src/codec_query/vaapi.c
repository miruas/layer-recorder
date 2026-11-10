#include "../../include/codec_query/vaapi.h"
#include "../../include/log.h"
#include "../../include/utils.h"

#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>

#include <va/va.h>
#include <va/va_drm.h>

static bool profile_is_h264(VAProfile profile) {
    switch(profile) {
        case 5: // VAProfileH264Baseline
        case VAProfileH264Main:
        case VAProfileH264High:
        case VAProfileH264ConstrainedBaseline:
            return true;
        default:
            return false;
    }
}

static bool profile_is_hevc_8bit(VAProfile profile) {
    switch(profile) {
        case VAProfileHEVCMain:
            return true;
        default:
            return false;
    }
}

static bool profile_is_hevc_10bit(VAProfile profile) {
    switch(profile) {
        case VAProfileHEVCMain10:
        //case VAProfileHEVCMain12:
        //case VAProfileHEVCMain422_10:
        //case VAProfileHEVCMain422_12:
        //case VAProfileHEVCMain444:
        //case VAProfileHEVCMain444_10:
        //case VAProfileHEVCMain444_12:
            return true;
        default:
            return false;
    }
}

static bool profile_is_hevc_yuv444_8bit(VAProfile profile) {
    return profile == VAProfileHEVCMain444;
}

static bool profile_is_hevc_yuv444_10bit(VAProfile profile) {
    return profile == VAProfileHEVCMain444_10;
}

static bool profile_is_av1(VAProfile profile) {
    switch(profile) {
        case VAProfileAV1Profile0:
        case VAProfileAV1Profile1:
            return true;
        default:
            return false;
    }
}

static bool profile_is_vp8(VAProfile profile) {
    switch(profile) {
        case VAProfileVP8Version0_3:
            return true;
        default:
            return false;
    }
}

static bool profile_is_vp9(VAProfile profile) {
    switch(profile) {
        case VAProfileVP9Profile0:
        case VAProfileVP9Profile1:
        case VAProfileVP9Profile2:
        case VAProfileVP9Profile3:
            return true;
        default:
            return false;
    }
}

/* Returns 0, 0 on error */
static vec2i profile_entrypoint_get_max_resolution(VADisplay va_dpy, VAProfile profile, VAEntrypoint entrypoint) {
    VAConfigAttrib attribs[2] = {
        {
            .type = VAConfigAttribMaxPictureWidth,
        },
        {
            .type = VAConfigAttribMaxPictureHeight,
        }
    };
    if(vaGetConfigAttributes(va_dpy, profile, entrypoint, attribs, 2) != VA_STATUS_SUCCESS || (attribs[0].value & VA_ATTRIB_NOT_SUPPORTED) || (attribs[1].value & VA_ATTRIB_NOT_SUPPORTED))
        return (vec2i){0, 0};

    return (vec2i){ attribs[0].value, attribs[1].value };
}

/* Returns 0 on error or if none is supported */
static VAEntrypoint profile_get_video_encoding_entrypoint(VADisplay va_dpy, VAProfile profile) {
    int num_entrypoints = vaMaxNumEntrypoints(va_dpy);
    if(num_entrypoints <= 0)
        return 0;

    VAEntrypoint *entrypoint_list = calloc(num_entrypoints, sizeof(VAEntrypoint));
    if(!entrypoint_list)
        return 0;

    int encoding_entrypoint_index = -1;
    int lower_power_entrypoint_index = -1;
    if(vaQueryConfigEntrypoints(va_dpy, profile, entrypoint_list, &num_entrypoints) == VA_STATUS_SUCCESS) {
        for(int i = 0; i < num_entrypoints; ++i) {
            if(entrypoint_list[i] == VAEntrypointEncSlice)
                encoding_entrypoint_index = i;
            else if(entrypoint_list[i] == VAEntrypointEncSliceLP)
                lower_power_entrypoint_index = i;
        }
    }

    VAEntrypoint encoding_entrypoint = 0;
    if(encoding_entrypoint_index != -1)
        encoding_entrypoint = entrypoint_list[encoding_entrypoint_index];
    else if(lower_power_entrypoint_index != -1)
        encoding_entrypoint = entrypoint_list[lower_power_entrypoint_index];

    free(entrypoint_list);
    return encoding_entrypoint;
}

static bool profile_is_known_codec(VAProfile profile) {
    return profile_is_h264(profile) || profile_is_hevc_8bit(profile) || profile_is_hevc_10bit(profile) || profile_is_hevc_yuv444_8bit(profile)
        || profile_is_hevc_yuv444_10bit(profile) || profile_is_av1(profile) || profile_is_vp8(profile) || profile_is_vp9(profile);
}

static gsr_supported_video_codec profile_get_supported_video_codec(VADisplay va_dpy, VAProfile profile) {
    const VAEntrypoint encoding_entrypoint = profile_get_video_encoding_entrypoint(va_dpy, profile);
    if(encoding_entrypoint == 0)
        return (gsr_supported_video_codec){0};

    return (gsr_supported_video_codec){
        .supported = true,
        .low_power = encoding_entrypoint == VAEntrypointEncSliceLP,
        .max_resolution = profile_entrypoint_get_max_resolution(va_dpy, profile, encoding_entrypoint),
    };
}

static bool get_supported_video_codecs(VADisplay va_dpy, gsr_supported_video_codecs *video_codecs, bool cleanup) {
    *video_codecs = (gsr_supported_video_codecs){0};
    bool success = false;
    VAProfile *profile_list = NULL;

    vaSetInfoCallback(va_dpy, NULL, NULL);

    int va_major = 0;
    int va_minor = 0;
    if(vaInitialize(va_dpy, &va_major, &va_minor) != VA_STATUS_SUCCESS) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_get_supported_video_codecs_vaapi: vaInitialize failed");
        return false;
    }

    int num_profiles = vaMaxNumProfiles(va_dpy);
    if(num_profiles <= 0)
        goto fail;

    profile_list = calloc(num_profiles, sizeof(VAProfile));
    if(!profile_list || vaQueryConfigProfiles(va_dpy, profile_list, &num_profiles) != VA_STATUS_SUCCESS)
        goto fail;

    gsr_supported_video_codec_yuv444 hevc_yuv444_8bit = {0};
    gsr_supported_video_codec_yuv444 hevc_yuv444_10bit = {0};

    for(int i = 0; i < num_profiles; ++i) {
        const VAProfile profile = profile_list[i];
        if(!profile_is_known_codec(profile))
            continue;

        const gsr_supported_video_codec supported_video_codec = profile_get_supported_video_codec(va_dpy, profile);
        if(!supported_video_codec.supported)
            continue;

        if(profile_is_h264(profile)) {
            video_codecs->h264 = supported_video_codec;
        } else if(profile_is_hevc_8bit(profile)) {
            video_codecs->hevc = supported_video_codec;
        } else if(profile_is_hevc_10bit(profile)) {
            video_codecs->hevc_hdr = supported_video_codec;
            video_codecs->hevc_10bit = supported_video_codec;
        } else if(profile_is_hevc_yuv444_8bit(profile)) {
            hevc_yuv444_8bit = (gsr_supported_video_codec_yuv444){ true, supported_video_codec.low_power };
        } else if(profile_is_hevc_yuv444_10bit(profile)) {
            hevc_yuv444_10bit = (gsr_supported_video_codec_yuv444){ true, supported_video_codec.low_power };
        } else if(profile_is_av1(profile)) {
            video_codecs->av1 = supported_video_codec;
            video_codecs->av1_hdr = supported_video_codec;
            video_codecs->av1_10bit = supported_video_codec;
        } else if(profile_is_vp8(profile)) {
            video_codecs->vp8 = supported_video_codec;
        } else if(profile_is_vp9(profile)) {
            video_codecs->vp9 = supported_video_codec;
        }
    }

    if(video_codecs->hevc.supported)
        video_codecs->hevc.yuv444 = hevc_yuv444_8bit;
    if(video_codecs->hevc_10bit.supported)
        video_codecs->hevc_10bit.yuv444 = hevc_yuv444_10bit;
    if(video_codecs->hevc_hdr.supported)
        video_codecs->hevc_hdr.yuv444 = hevc_yuv444_10bit;

    success = true;
    fail:
    if(profile_list)
        free(profile_list);

    if(cleanup)
        vaTerminate(va_dpy);

    return success;
}

bool gsr_get_supported_video_codecs_vaapi(gsr_supported_video_codecs *video_codecs, const char *card_path, bool cleanup) {
    memset(video_codecs, 0, sizeof(*video_codecs));
    bool success = false;
    int drm_fd = -1;

    char render_path[128];
    if(!gsr_card_path_get_render_path(card_path, render_path)) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_get_supported_video_codecs_vaapi: failed to get /dev/dri/renderDXXX file from %s", card_path);
        goto done;
    }

    drm_fd = open(render_path, O_RDWR);
    if(drm_fd == -1) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_get_supported_video_codecs_vaapi: failed to open device %s", render_path);
        goto done;
    }

    VADisplay va_dpy = vaGetDisplayDRM(drm_fd);
    if(va_dpy) {
        if(!get_supported_video_codecs(va_dpy, video_codecs, cleanup)) {
            gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_get_supported_video_codecs_vaapi: failed to query supported video codecs for device %s", render_path);
            goto done;
        }
        success = true;
    }

    done:
    if(cleanup) {
        if(drm_fd > 0)
            close(drm_fd);
    }

    return success;
}
