#ifndef GSR_FFMPEG_UTILS_H
#define GSR_FFMPEG_UTILS_H

#include <stdbool.h>

typedef struct AVFormatContext AVFormatContext;
typedef struct AVFrame AVFrame;
typedef struct AVBufferRef AVBufferRef;

const char* gsr_av_error_to_string(int err);

/* Marks that a packet has been successfully written to |av_format_context|, see gsr_av_format_context_write_trailer */
void gsr_av_format_context_mark_packet_written(AVFormatContext *av_format_context);
/*
    The same as av_write_trailer, except that the trailer is not written when no packet has been written to a muxer
    that uses the hybrid_fragmented movflags option (see set_format_context_options), because the mov muxer in FFmpeg
    crashes when it finalizes a hybrid_fragmented file that has no packets. Returns 0 on success, just like av_write_trailer.
*/
int gsr_av_format_context_write_trailer(AVFormatContext *av_format_context);

AVFrame* gsr_hw_frame_alloc(AVBufferRef *hw_frames_ctx);
/*
    Replaces the hardware buffer of |frame| with a new one from its frame pool while keeping the frame properties (pts, color properties and side data).
    Encoders keep a reference to the frames that are sent to them until they have been encoded (which can be several frames later with b-frames),
    so writing to the same buffer every frame overwrites frames that haven't been encoded yet.
*/
bool gsr_frame_replace_hw_buffer(AVFrame *frame);
void gsr_frame_move_hw_buffer(AVFrame *dst, AVFrame *src);

#endif /* GSR_FFMPEG_UTILS_H */
