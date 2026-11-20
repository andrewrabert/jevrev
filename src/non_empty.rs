use std::num::NonZeroUsize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NonEmpty<T> {
    first: T,
    rest: Vec<T>,
}

impl<T> NonEmpty<T> {
    pub fn new(first: T, rest: Vec<T>) -> NonEmpty<T> {
        NonEmpty { first, rest }
    }

    pub fn from_vec(mut items: Vec<T>) -> Option<NonEmpty<T>> {
        if items.is_empty() {
            return None;
        }
        let first = items.remove(0);
        Some(NonEmpty { first, rest: items })
    }

    pub fn len(&self) -> NonZeroUsize {
        NonZeroUsize::MIN.saturating_add(self.rest.len())
    }

    pub fn first(&self) -> &T {
        &self.first
    }

    pub fn last(&self) -> &T {
        self.rest.last().unwrap_or(&self.first)
    }

    pub fn iter(&self) -> impl Iterator<Item = &T> {
        std::iter::once(&self.first).chain(&self.rest)
    }

    pub fn map<U>(&self, mut f: impl FnMut(&T) -> U) -> NonEmpty<U> {
        NonEmpty {
            first: f(&self.first),
            rest: self.rest.iter().map(f).collect(),
        }
    }
}
