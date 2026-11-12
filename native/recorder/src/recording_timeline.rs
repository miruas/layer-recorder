// SPDX-License-Identifier: GPL-3.0-only
//! Ordinary-recording edits on the continuous replay clock.
//! A pause excludes an interval from the recording timeline; independent RAM
//! replay continues. Originals stay non-destructive, as with editor trims.
#[derive(Default, Debug)]
pub struct RecordingTimeline {
    spans: Vec<(f64, Option<f64>)>,
}
impl RecordingTimeline {
    pub fn begin(&mut self, at: f64) { self.spans = vec![(at, None)]; }
    pub fn clear(&mut self) { self.spans.clear(); }
    pub fn paused(&self) -> bool { self.spans.last().is_some_and(|(_, end)| end.is_some()) }
    pub fn set_paused(&mut self, paused: bool, at: f64) {
        if self.spans.is_empty() || self.paused() == paused { return; }
        if paused { self.spans.last_mut().unwrap().1 = Some(at); }
        else { self.spans.push((at, None)); }
    }
    pub fn spans(&self, start: f64, end: f64) -> Vec<(f64, f64)> {
        self.spans.iter().filter_map(|(a,b)| {
            let a=a.max(start); let b=b.unwrap_or(end).min(end);
            (b-a > 0.001).then_some((a,b))
        }).collect()
    }
    pub fn duration(&self, start: f64, end: f64) -> f64 {
        self.spans(start,end).iter().map(|(a,b)|b-a).sum()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_pause_events_are_idempotent_and_pause_at_stop_stays_excluded() {
        let mut t=RecordingTimeline::default(); t.begin(5.);
        t.set_paused(true,7.); t.set_paused(true,8.);
        assert_eq!(t.spans(5.,10.),vec![(5.,7.)]);
        t.set_paused(false,11.); t.set_paused(false,12.);
        assert_eq!(t.spans(5.,14.),vec![(5.,7.),(11.,14.)]);
        assert_eq!(t.duration(5.,14.),5.);
    }
    #[test]
    fn rapid_pause_resume_and_empty_timeline_do_not_create_negative_sections() {
        let mut t=RecordingTimeline::default(); t.set_paused(true,3.);
        assert!(t.spans(0.,5.).is_empty()); t.begin(2.);
        t.set_paused(true,2.);t.set_paused(false,2.);
        assert_eq!(t.spans(0.,3.),vec![(2.,3.)]); t.clear();
        assert!(!t.paused());assert_eq!(t.duration(0.,9.),0.);
    }
}
