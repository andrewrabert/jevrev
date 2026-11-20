//! Crate doc.

use std::fmt;

/// A constant.
const LIMIT: u32 = 10;
static NAME: &str = "x";

#[derive(Debug)]
pub struct Point {
    x: i32,
}

impl Point {
    /// New.
    pub fn new(x: i32) -> Self {
        Point { x }
    }

    pub fn x(&self) -> i32 {
        self.x
    }
}

pub trait Shape {
    fn area(&self) -> f64;
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.x)
    }
}

mod inner {
    pub fn helper() {}

    #[cfg(test)]
    fn skipped() {}
}

#[cfg(test)]
mod tests {
    #[test]
    fn it_works() {}
}
