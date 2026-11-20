set default-list := true

[group: 'test']
test:
  cargo test --tests --all --all-targets

[group: 'check']
check:
  cargo check --tests --all --all-targets

[group: 'check']
ci: test
  cargo clippy --all --all-targets --all-features -- --deny warnings
  cargo fmt --all -- --check
  cargo update --locked --package jevrev

[group: 'misc']
run:
  cargo run

[group: 'misc']
build:
  cargo build

[group: 'misc']
fmt:
  cargo fmt --all

[group: 'check']
outdated:
  cargo outdated --root-deps-only --exit-code 1

# install jevrev from this checkout
[group: 'misc']
install:
  cargo install --force --path .

[group: 'check']
clippy:
  cargo clippy --all --all-targets --all-features -- --deny warnings
