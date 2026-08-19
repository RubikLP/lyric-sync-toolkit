#!/usr/bin/env python3
"""Entry point for the lyric-sync toolkit. Each pipeline step is its own subcommand."""

import argparse

from src.lyric_sync import align, detect_unsynced, fetch, relocate, verify_density, verify_duration, verify_source


def main():
    parser = argparse.ArgumentParser(prog="lyric-sync", description="Lyric fetching, alignment and verification toolkit.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch.add_subparser(subparsers)
    relocate.add_subparser(subparsers)
    detect_unsynced.add_subparser(subparsers)
    verify_source.add_subparser(subparsers)
    align.add_subparser(subparsers)
    verify_duration.add_subparser(subparsers)
    verify_density.add_subparser(subparsers)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
