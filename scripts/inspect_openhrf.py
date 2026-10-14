"""Inspect actual RF schema and representative frames in each local acquisition."""
import argparse
from pathlib import Path

from ivus import h5py, metadata, np, read_frame, timing, write_json


# Describe one acquisition using its settings and three example frames.
def inspect(path, track=None):

    # Open the source and read the selected track settings.
    with h5py.File(path, "r") as file:
        meta = metadata(file, track)
        frames, lines, samples = meta["shape"][:3]
        stats = []

        # Inspect the first, middle, and last frame only once each.
        for index in sorted({0, frames // 2, frames - 1}):
            frame = read_frame(file, meta, index)

            # Record signal size and average offsets for this frame.
            stats.append({"frame": index, "minimum": float(frame.min()), "maximum": float(frame.max()),
                          "mean": float(frame.mean()), "rms": float(np.sqrt(np.mean(frame ** 2))),
                          "maximum_absolute_line_mean": float(np.max(np.abs(frame.mean(axis=-1))))})

        # Report the source, scan coverage, payload size, and playback timing.
        return {"source": str(path.resolve()), "stored_bytes": path.stat().st_size,
                "metadata": meta, "representative_frame_statistics": stats,

                # Make clear that these examples are not a complete validity check.
                "statistics_coverage": "first, middle, last frames; not a full RF validity scan",
                "expected_playback_bytes": frames * lines * (samples // 8) * 2,
                "timing": timing(meta, 0, frames)}


# Read command-line choices and save an inspection report.
def main():

    # Choose the input, report destination, and optional RF track.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="HDF5 file or acquisition directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--track")
    args = parser.parse_args()

    # Use every HDF5 acquisition in a directory, or the single input file.
    paths = sorted(args.input.glob("*.hdf5")) if args.input.is_dir() else [args.input]

    # Stop when the directory contains no acquisitions.
    if not paths:
        parser.error("No HDF5 acquisitions found")

    # Collect one report per acquisition.
    reports = []

    # Inspect each source and show a short progress message.
    for path in paths:
        reports.append(inspect(path, args.track))
        meta = reports[-1]["metadata"]
        print(f"{path.name}: {meta['shape'][:3]}, {meta['frame_rate_hz']:.6g} Hz, rotation {meta['rotation_sign']:+d}", flush=True)

    # Save all acquisition reports together.
    write_json(args.output, {"acquisitions": reports})


# Run the command only when this file is launched directly.
if __name__ == "__main__":
    main()
