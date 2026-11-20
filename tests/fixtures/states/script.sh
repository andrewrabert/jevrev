#!/bin/sh
# Script comment.
set -eu
trap 'rm -f "$tmp"' EXIT

tmp=$(mktemp)

# Greets.
greet() {
    echo "hello $1"
}

greet world
echo done
