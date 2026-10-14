"""Shared offline IVUS schema, conditioning, and playback timing utilities."""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import hdf5plugin  # noqa: F401 -- enables reading compressed HDF5 samples
import h5py
import numpy as np
from scipy import signal

# Use the board clock and the signed 14-bit sample limit.
TARGET_FS = 125_000_000
LIMIT = 8191
FORMAT = "doppler-dynamics-ivus-playback-v1"


# Read one stored value as a normal Python number or string.
def scalar(file, name, default=None):

    # Use the fallback when this optional value is missing.
    if name not in file:
        return default

    # Read the value and check that it contains only one item.
    value = file[name][()]

    # Reject an array where a single value is required.
    if np.asarray(value).size != 1:
        raise ValueError(f"Expected scalar at {name}")

    # Unwrap the stored number or decode its text.
    value = np.asarray(value).reshape(-1)[0]
    return value.decode("utf-8") if isinstance(value, bytes) else value.item() if isinstance(value, np.generic) else value


# Find exactly one location containing the recorded RF samples.
def find_layout(file, track=None):
    candidates = []

    # Check for samples stored directly at the file root.
    if "data/raw_data" in file:
        candidates.append("")

    # Check each named track for its own RF samples.
    if "tracks" in file:
        candidates.extend(f"tracks/{name}/" for name in file["tracks"] if f"tracks/{name}/data/raw_data" in file)

    # Keep only the track requested by the caller.
    if track is not None:
        candidates = [prefix for prefix in candidates if prefix == f"tracks/{track}/"]

    # Reject missing or ambiguous tracks rather than guessing.
    if len(candidates) != 1:
        raise ValueError(f"Select one RF track with --track; available layouts: {candidates}")

    # Return the matching sample, scan, and image locations.
    prefix = candidates[0]
    return prefix + "data/raw_data", prefix + "scan/", prefix + "data/"


# Read and check the information needed to process an acquisition.
def metadata(file, track=None):

    # Find the selected track and its sample array.
    raw_path, scan_path, data_path = find_layout(file, track)
    raw = file[raw_path]

    # Require frames of lines with two unused size-one dimensions.
    if len(raw.shape) != 5 or raw.shape[-2:] != (1, 1) or any(d <= 0 for d in raw.shape):
        raise ValueError(f"Expected nonempty [frame,line,sample,1,1] RF, found {raw.shape}")

    # Read the sample rate, signal frequency, and frame rate.
    fs = float(scalar(file, scan_path + "sampling_frequency"))
    fc = float(scalar(file, scan_path + "center_frequency"))
    rate = float(scalar(file, "metadata/rotation/sampling_frequency"))

    # Reject missing, invalid, or nonpositive frequencies.
    if not all(np.isfinite(v) and v > 0 for v in (fs, fc, rate)):
        raise ValueError("Sampling, center and frame frequencies must be finite and positive")
    # Support this dataset's 1 GHz source and 125 MHz playback rate.
    if fs != 1_000_000_000 or fc >= 50_000_000:
        raise ValueError(f"Unsupported RF frequency configuration: fs={fs}, fc={fc}")

    # Require whole groups of eight samples for downsampling.
    if raw.shape[2] % 8:
        raise ValueError("Source line length must be divisible by 8")

    # Collect the angle and acoustic time offsets for every line.
    arrays = {}

    # Read each line setting in the original line order.
    for name in ("polar_angles", "initial_times", "t0_delays"):
        values = np.asarray(file[scan_path + name][()], dtype=np.float64).reshape(-1)

        # Require one finite setting for every recorded line.
        if len(values) != raw.shape[1] or not np.all(np.isfinite(values)):
            raise ValueError(f"Invalid per-line metadata: {name}")
        arrays[name] = values.tolist()

    # Read the direction of rotation without reversing the samples.
    rotation = scalar(file, "metadata/rotation/samples")

    # Only the two known rotation directions are accepted.
    if rotation not in (-1, 1):
        raise ValueError("Expected rotation sign -1 or +1")

    # Leave pullback information empty when the file does not provide it.
    pullback = None

    # Read the probe position for each frame when available.
    if "metadata/pullback_position/samples" in file:
        values = np.asarray(file["metadata/pullback_position/samples"][()], dtype=np.float64).reshape(-1)

        # Require one finite probe position per frame.
        if len(values) != raw.shape[0] or not np.all(np.isfinite(values)):
            raise ValueError("Invalid pullback position metadata")

        # Keep the positions with their timing information.
        pullback = {"positions_m": values.tolist(),
                    "sampling_frequency_hz": scalar(file, "metadata/pullback_position/sampling_frequency"),
                    "start_time_offset_s": scalar(file, "metadata/pullback_position/start_time_offset")}

    # Describe where the samples live and how they are stored.
    return {"raw_path": raw_path, "scan_path": scan_path, "data_path": data_path,
            "zea_version": str(file.attrs.get("zea_version", "unknown")),
            "shape": list(raw.shape), "dtype": str(raw.dtype), "chunks": list(raw.chunks) if raw.chunks else None,
            "compression_filters": {key: list(value) if isinstance(value, tuple) else value for key, value in raw._filters.items()},

            # Record the signal frequencies and frame timing.
            "source_sampling_frequency_hz": fs, "center_frequency_hz": fc,
            "frame_rate_hz": rate, "frame_rate_source": "metadata/rotation/sampling_frequency",
            "frame_time_origin_s": scalar(file, "metadata/rotation/start_time_offset", 0.0),

            # Keep the direction, source credit, and optional probe positions.
            "rotation_sign": int(rotation), "rotation_description": scalar(file, "metadata/text_report"),
            "credit": scalar(file, "metadata/credit"), "pullback": pullback,

            # Keep the original angles and acoustic offsets for reconstruction.
            "scan": arrays}


# Load one frame of lines without loading the whole acquisition.
def read_frame(file, meta, index):
    frame = np.asarray(file[meta["raw_path"]][index, :, :, 0, 0], dtype=np.float64)

    # Reject a frame containing missing or infinite sample values.
    if not np.all(np.isfinite(frame)):
        raise ValueError(f"Nonfinite RF samples in source frame {index}")
    return frame


# Build the filter that removes frequencies too high for playback.
def make_filter():
    count, beta = signal.kaiserord(80, 12_500_000 / 500_000_000)

    # Use an odd number of taps so the filter has a centered sample.
    count |= 1
    return signal.firwin(count, 56_250_000, window=("kaiser", beta), fs=1_000_000_000)


# Filter each line separately and reduce its sample rate by eight.
def condition(frame, taps):
    converted = signal.resample_poly(frame, 1, 8, axis=-1, window=taps, padtype="constant")

    # Remove each line's average offset after resampling.
    return converted - converted.mean(axis=-1, keepdims=True)


# Apply one gain and round samples to whole ADC counts.
def quantize(frame, gain):
    rounded = np.rint(frame * gain)

    # Count out-of-range samples before limiting their values.
    clipped = int(np.count_nonzero((rounded < -LIMIT) | (rounded > LIMIT)))

    # Store signed values in two-byte little-endian words.
    return np.clip(rounded, -LIMIT, LIMIT).astype("<i2"), clipped


# Describe when each line starts on the 125 MHz playback clock.
def timing(meta, frame_start, frame_count):
    lines = meta["shape"][1]
    # Keep the exact fractional period so playback timing does not drift.
    # Use the stored float directly to keep its fraction within register limits.
    period = Fraction(TARGET_FS, 1) / Fraction(meta["frame_rate_hz"]) / lines

    # Keep both parts of the period within 64-bit loader registers.
    if max(period.numerator, period.denominator) >= 2**64:
        raise ValueError("Frame cadence cannot be represented in 64-bit timing registers")

    # Check that the active samples fit before the next line starts.
    samples = meta["shape"][2] // 8

    # Reject a frame rate that leaves too little time for each line.
    if period < samples:
        raise ValueError("A-line active duration exceeds its scheduled interval")

    # Store the exact fractional period and the rounding rule.
    return {"clock_hz": TARGET_FS,
            "line_period_ticks_numerator": period.numerator,
            "line_period_ticks_denominator": period.denominator,
            "line_start_tick_rule": "floor(global_line_index * numerator / denominator + 1/2)",

            # Count lines across replay loops so rounding does not build up drift.
            "global_line_index": "replay_loop * frame_count * lines_per_frame + local_frame * lines_per_frame + line",
            "line_period_s": float(period / TARGET_FS),
            "active_line_duration_s": samples / TARGET_FS,

            # State that line spacing is inferred from the recorded rotation rate.
            "assumption": "Uniform line spacing inferred from rotation sampling frequency; acoustic initial_times are not acquisition timestamps",

            # Locate the selected frames on the original acquisition timeline.
            "source_first_frame": frame_start, "loop_frame_count": frame_count,
            "source_first_frame_time_s": meta["frame_time_origin_s"] + frame_start / meta["frame_rate_hz"],

            # Keep idle time out of the payload and mark each replay boundary.
            "idle_samples": "invalid; omit from storage and network payload",
            "loop_boundary": "emit acquisition/loop marker; global replay clock does not reset"}


# Make a checksum for detecting changed or damaged files.
def sha256(path):
    digest = hashlib.sha256()

    # Read the binary without loading it all into memory.
    with Path(path).open("rb") as stream:

        # Update the checksum one megabyte at a time.
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Save a readable report through a temporary file.
def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Finish writing before replacing the final report.
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
