#include "../include/ffmpeg_utils.h"
#include "../include/log.h"

#include <string.h>
#include <libavutil/error.h>
#include <libavutil/opt.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>

static _Thread_local char av_error_buffer[AV_ERROR_MAX_STRING_SIZE];

const char* gsr_av_error_to_string(int err) {
    if(av_strerror(err, av_error_buffer, sizeof(av_error_buffer)) < 0)
        strcpy(av_error_buffer, "Unknown error");
    return av_error_buffer;
}

void gsr_av_format_context_mark_packet_written(AVFormatContext *av_format_context) {
    av_format_context->opaque = (void*)1;
}

static bool av_format_context_uses_hybrid_fragmented(AVFormatContext *av_format_context) {
    if(LIBAVFORMAT_VERSION_INT < AV_VERSION_INT(62, 6, 101))
        return false;

    const AVOption *opt = av_opt_find(av_format_context->priv_data, "movflags", NULL, 0, 0);
    if(!opt || !opt->unit)
        return false;

    return av_opt_find(av_format_context->priv_data, "hybrid_fragmented", opt->unit, 0, 0) != NULL;
}

int gsr_av_format_context_write_trailer(AVFormatContext *av_format_context) {
    const bool packet_written = av_format_context->opaque != NULL;
    if(!packet_written && av_format_context_uses_hybrid_fragmented(av_format_context)) {
        gsr_log(GSR_LOG_LEVEL_WARNING, "not finalizing the video file because it has no video/audio data");
        return 0;
    }

    return av_write_trailer(av_format_context);
}

AVFrame* gsr_hw_frame_alloc(AVBufferRef *hw_frames_ctx) {
    AVFrame *frame = av_frame_alloc();
    if(!frame)
        return NULL;

    const int res = av_hwframe_get_buffer(hw_frames_ctx, frame, 0);
    if(res < 0) {
        gsr_log(GSR_LOG_LEVEL_ERROR, "gsr_hw_frame_alloc: av_hwframe_get_buffer failed: %s", gsr_av_error_to_string(res));
        av_frame_free(&frame);
        return NULL;
    }

    return frame;
}

bool gsr_frame_replace_hw_buffer(AVFrame *frame) {
    AVFrame *new_frame = gsr_hw_frame_alloc(frame->hw_frames_ctx);
    if(!new_frame)
        return false;

    gsr_frame_move_hw_buffer(frame, new_frame);
    av_frame_free(&new_frame);
    return true;
}

void gsr_frame_move_hw_buffer(AVFrame *dst, AVFrame *src) {
    for(int i = 0; i < AV_NUM_DATA_POINTERS; ++i) {
        av_buffer_unref(&dst->buf[i]);
        dst->buf[i] = src->buf[i];
        src->buf[i] = NULL;
        dst->data[i] = src->data[i];
        dst->linesize[i] = src->linesize[i];
    }
}
