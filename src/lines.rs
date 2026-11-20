use std::num::NonZeroUsize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct LineRange {
    start: NonZeroUsize,
    end: NonZeroUsize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("line range {start}-{end} ends before it starts")]
pub struct LineRangeError {
    pub start: NonZeroUsize,
    pub end: NonZeroUsize,
}

impl LineRange {
    pub fn new(start: NonZeroUsize, end: NonZeroUsize) -> Result<LineRange, LineRangeError> {
        if end < start {
            return Err(LineRangeError { start, end });
        }
        Ok(LineRange { start, end })
    }

    pub fn line(line: NonZeroUsize) -> LineRange {
        LineRange {
            start: line,
            end: line,
        }
    }

    pub(crate) fn rows(first: usize, last: usize) -> Result<LineRange, LineRangeError> {
        LineRange::new(
            NonZeroUsize::MIN
                .checked_add(first)
                .expect("a row's line number fits in usize"),
            NonZeroUsize::MIN
                .checked_add(last)
                .expect("a row's line number fits in usize"),
        )
    }

    pub fn start(&self) -> NonZeroUsize {
        self.start
    }

    pub fn end(&self) -> NonZeroUsize {
        self.end
    }

    pub fn line_numbers(&self) -> impl Iterator<Item = NonZeroUsize> {
        (self.start.get()..=self.end.get()).filter_map(NonZeroUsize::new)
    }

    pub fn through(&self, other: &LineRange) -> LineRange {
        LineRange {
            start: self.start,
            end: self.end.max(other.end),
        }
    }

    pub fn intersection(&self, other: &LineRange) -> Option<LineRange> {
        LineRange::new(self.start.max(other.start), self.end.min(other.end)).ok()
    }

    pub fn before(&self, line: NonZeroUsize) -> Option<LineRange> {
        let last = NonZeroUsize::new(line.get() - 1)?;
        LineRange::new(self.start, self.end.min(last)).ok()
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
struct Stored {
    start: NonZeroUsize,
    end: NonZeroUsize,
}

impl serde::Serialize for LineRange {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        Stored {
            start: self.start,
            end: self.end,
        }
        .serialize(serializer)
    }
}

impl<'de> serde::Deserialize<'de> for LineRange {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<LineRange, D::Error> {
        let stored = Stored::deserialize(deserializer)?;
        LineRange::new(stored.start, stored.end).map_err(serde::de::Error::custom)
    }
}
