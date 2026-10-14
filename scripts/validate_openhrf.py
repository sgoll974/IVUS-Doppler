"""Read exported bytes back, compare all selected RF, and generate fidelity previews."""
import argparse
import json
from pathlib import Path

import matplotlib

# Save plots to image files without opening a display window.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import ndimage, signal

from ivus import (FORMAT, LIMIT, TARGET_FS, condition, h5py, metadata, np,
                  read_frame, sha256, timing, write_json)


# Measure the frequencies that the filter keeps and removes.
def filter_measurements(taps):
    frequencies, response = signal.freqz(taps, worN=131072, fs=1_000_000_000)

    # Convert the filter response magnitude to decibels.
    db = 20 * np.log10(np.maximum(np.abs(response), 1e-15))

    # Check up to 50 MHz for signal loss and from 62.5 MHz for rejection.
    return {"maximum_passband_deviation_db": float(np.max(np.abs(db[frequencies <= 50_000_000]))),
            "minimum_stopband_attenuation_db": float(-np.max(db[frequencies >= 62_500_000]))}


# Find the signal outline after keeping the 20 to 50 MHz band.
def envelope(rf, fs):
    band = signal.butter(4, [20_000_000, 50_000_000], btype="bandpass", fs=fs, output="sos")

    # Filter forward and backward to avoid shifting the waveform.
    return np.abs(signal.hilbert(signal.sosfiltfilt(band, rf, axis=-1), axis=-1))


# Place the signal outline on the stored image coordinate grid.
def cartesian_preview(env, coordinates, angles, fs):
    """Visualization only: assumed sound speed 1540 m/s, stored xy coordinates."""

    # Work out the angle step between neighboring lines.
    spacing = np.diff(angles)
    step = 2 * np.pi / len(angles)

    # Reject angles that do not match a uniform ascending scan.
    if not np.allclose(spacing, step, rtol=0, atol=1e-5):
        raise ValueError("Preview requires uniformly ascending polar angles")

    # Convert each image position to its angle and distance from the probe.
    theta = np.arctan2(coordinates[..., 1], coordinates[..., 0])
    radius = np.hypot(coordinates[..., 0], coordinates[..., 1])

    # Find the scan line and sample for each image position.
    line_index = ((theta - angles[0]) % (2 * np.pi)) / step

    # Use the sound travel time out and back to convert distance to samples.
    sample_index = radius * (2 * fs / 1540.0)
    # Repeat the first line so interpolation joins the scan edges smoothly.
    wrapped = np.concatenate([env, env[:1]], axis=0)
    image = ndimage.map_coordinates(wrapped, [line_index, sample_index], order=1,
                                   mode="constant", cval=0, prefilter=False)

    # Show brightness relative to the peak, within a 60 dB display range.
    db = 20 * np.log10(np.maximum(image / max(float(image.max()), 1e-20), 1e-5))
    return np.clip(db, -60, 0)


# Read the binary back and compare it with the recorded source.
def validate(manifest_path, report_path, previews=True):

    # Load the manifest and locate the declared binary.
    manifest_path, report_path = Path(manifest_path).resolve(), Path(report_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Require the supported format and signed little-endian words.
    if manifest["format"] != FORMAT or manifest["binary"]["dtype"] != "<i2":
        raise ValueError("Unsupported playback format or sample encoding")
    binary = manifest_path.parent / manifest["binary"]["file"]
    shape = tuple(manifest["binary"]["shape"])

    # Require positive frame, line, and sample counts.
    if len(shape) != 3 or any(not isinstance(d, int) or d <= 0 for d in shape):
        raise ValueError("Invalid output dimensions")

    # Reject missing or extra sample bytes.
    if binary.stat().st_size != int(np.prod(shape)) * 2 or binary.stat().st_size != manifest["binary"]["bytes"]:
        raise ValueError("Binary length does not match the manifest")

    # Reject a file whose checksum has changed.
    if sha256(binary) != manifest["binary"]["sha256"]:
        raise ValueError("Binary checksum mismatch")

    # Read samples from disk as needed rather than loading the whole binary.
    words = np.memmap(binary, mode="r", dtype="<i2", shape=shape)
    taps = np.asarray(manifest["conditioning"]["fir_taps"], dtype=float)

    # Require a finite, centered filter with an odd number of taps.
    if not np.all(np.isfinite(taps)) or len(taps) % 2 == 0 or not np.allclose(taps, taps[::-1], atol=1e-14):
        raise ValueError("Expected a finite symmetric odd-length FIR")

    # Read the one fixed gain used for the exported acquisition.
    gain = manifest["conditioning"]["gain_adc_counts_per_source_unit"]

    # Reject a negative or invalid gain.
    if not np.isfinite(gain) or gain < 0:
        raise ValueError("Invalid quantization gain")

    # Measure the declared filter before checking the samples.
    measurements = filter_measurements(taps)

    # Allow at most 0.02 dB signal change and require 70 dB rejection.
    checks = {"checksum": True, "byte_length": True,
              "filter_passband_within_0_02_db": measurements["maximum_passband_deviation_db"] <= 0.02,
              "filter_stopband_at_least_70_db": measurements["minimum_stopband_attenuation_db"] >= 70}

    # Start counters for the full sample comparison.
    stats = {"minimum": LIMIT, "maximum": -LIMIT, "maximum_absolute_line_mean_counts": 0.0,
             "maximum_quantization_error_counts": 0.0, "samples_compared": 0,
             "clipped_samples": 0, "processed_peak": 0.0}

    # Choose the first, middle, and last exported frames for image checks.
    comparisons, preview_paths = [], []
    start, stop = manifest["source"]["frame_start"], manifest["source"]["frame_stop_exclusive"]
    selected = {0, shape[0] // 2, shape[0] - 1}

    # Reopen the source track named in the manifest.
    with h5py.File(manifest["source"]["path"], "r") as file:
        stored = manifest["source"]["metadata"]
        track = stored["raw_path"].split("/")[1] if stored["raw_path"].startswith("tracks/") else None
        meta = metadata(file, track)

        # Check that the source selection matches the binary dimensions.
        if not 0 <= start < stop <= meta["shape"][0] or shape != (stop-start, meta["shape"][1], meta["shape"][2] // 8):
            raise ValueError("Source frame selection or dimensions do not match")

        # Require matching source settings, including selected probe positions.
        if stored != dict(meta, pullback=(dict(meta["pullback"], positions_m=meta["pullback"]["positions_m"][start:stop]) if meta["pullback"] else None)):
            raise ValueError("Source metadata does not match manifest")

        # Recalculate the fractional timing and verify the eightfold rate change.
        checks["timing_metadata"] = manifest["timing"] == timing(meta, start, shape[0])
        config = manifest["conditioning"]
        checks["sample_rate"] = (config["output_rate_hz"] == TARGET_FS
                                 and config["source_rate_hz"] == meta["source_sampling_frequency_hz"]
                                 and (config["up"], config["down"]) == (1, 8))

        # Use the original line angles for any reconstruction previews.
        angles = np.asarray(meta["scan"]["polar_angles"])

        # Compare every exported frame with freshly conditioned source samples.
        for local in range(shape[0]):
            frame = read_frame(file, meta, start + local)
            converted = condition(frame, taps)
            scaled = converted * gain
            decoded = np.asarray(words[local])

            # Track the signed range and the largest line offset.
            stats["minimum"] = min(stats["minimum"], int(decoded.min()))
            stats["maximum"] = max(stats["maximum"], int(decoded.max()))
            stats["maximum_absolute_line_mean_counts"] = max(stats["maximum_absolute_line_mean_counts"], float(np.max(np.abs(decoded.mean(axis=-1)))))

            # Allow only rounding error and count any samples that would clip.
            stats["maximum_quantization_error_counts"] = max(stats["maximum_quantization_error_counts"], float(np.max(np.abs(decoded - scaled))))
            stats["samples_compared"] += decoded.size
            stats["clipped_samples"] += int(np.count_nonzero(np.abs(np.rint(scaled)) > LIMIT))
            stats["processed_peak"] = max(stats["processed_peak"], float(np.max(np.abs(converted))))

            # Compare signal outlines on three representative frames when gain is nonzero.
            if local in selected and gain > 0:
                native_env = envelope(frame, meta["source_sampling_frequency_hz"])
                exported_env = envelope(decoded.astype(float) / gain, TARGET_FS)
                # Compare the waveforms without the filter's edge effects: 256 ns at each end.
                a, b = native_env[:, 256:-256:8].ravel(), exported_env[:, 32:-32].ravel()
                correlation = float(np.corrcoef(a, b)[0, 1]) if np.std(a) and np.std(b) else None

                # Record the correlation and the excluded edge width.
                comparisons.append({"source_frame": start + local, "envelope_correlation": correlation,
                                    "reference": "native-rate 20-50 MHz bandpass + Hilbert envelope; sampled every 8th sample",
                                    "edge_exclusion_samples_at_125mhz": 32})

                # Make comparison pictures when previews were requested.
                if previews:

                    # Reject acoustic time offsets that this preview does not account for.
                    if any(meta["scan"]["initial_times"]) or any(meta["scan"]["t0_delays"]):
                        raise ValueError("Preview requires zero acoustic time offsets")

                    # Read the stored image grid and reconstruct both RF versions on it.
                    data = meta["data_path"]
                    coordinates = np.asarray(file[data + "image/coordinates"][()])
                    original = cartesian_preview(native_env, coordinates, angles, meta["source_sampling_frequency_hz"])
                    exported = cartesian_preview(exported_env, coordinates, angles, TARGET_FS)

                    # Load the dataset image and choose a destination for the comparison.
                    supplied = file[data + "image/values"][start + local]
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    picture = report_path.with_name(report_path.stem + f"_frame_{start + local:04d}.png")

                    # Place the two reconstructions beside the supplied B-mode image.
                    fig, axes = plt.subplots(1, 3, figsize=(12, 4), constrained_layout=True)

                    # Label the stored coordinate grid in millimeters.
                    extent = [float(coordinates[..., 0].min()) * 1000, float(coordinates[..., 0].max()) * 1000,
                              float(coordinates[..., 1].min()) * 1000, float(coordinates[..., 1].max()) * 1000]

                    # Give each panel its own brightness range, title, and axis labels.
                    for axis, values, title, vmin, vmax in zip(axes, [original, exported, supplied],
                            ["Native RF reconstruction", "Exported 125 MSPS RF", "Dataset supplied B-mode"], [-60, -60, 0], [0, 0, 255]):
                        axis.imshow(values, cmap="gray", vmin=vmin, vmax=vmax, origin="lower", extent=extent)
                        axis.set_title(title)
                        axis.set_xlabel("x (mm)")
                        axis.set_ylabel("y (mm)")

                    # Save the assumptions with the image and close the figure afterward.
                    fig.suptitle(f"Source frame {start + local}; preview assumes c=1540 m/s, 60 dB display range")
                    fig.savefig(picture, dpi=160)
                    plt.close(fig)
                    preview_paths.append(str(picture))

            # Show progress at the first, last, and every tenth frame.
            if local == 0 or (local + 1) % 10 == 0 or local == shape[0] - 1:
                print(f"Verified exported frame {local + 1}/{shape[0]}", flush=True)

    # Require the signed range, half-count accuracy, and no clipping.
    checks.update({"signed_range": -LIMIT <= stats["minimum"] <= stats["maximum"] <= LIMIT,
                   "line_dc_within_half_count": stats["maximum_absolute_line_mean_counts"] <= 0.5,
                   # Leave a tiny allowance for floating-point math at the half-count limit.
                   "all_samples_match_source_within_half_count": stats["maximum_quantization_error_counts"] <= 0.50000001,
                   "no_clipping": stats["clipped_samples"] == 0})

    # Check that one fixed gain used the requested amplitude headroom.
    desired_peak = LIMIT * (1 - manifest["conditioning"]["headroom_fraction"])
    checks["fixed_gain_matches_processed_peak"] = bool(np.isclose(stats["processed_peak"] * gain, desired_peak, rtol=1e-12)) if stats["processed_peak"] else gain == 0

    # Require at least 0.99 correlation for every compared signal outline.
    checks["representative_envelope_correlation_at_least_0_99"] = all(c["envelope_correlation"] is not None and c["envelope_correlation"] >= 0.99 for c in comparisons)

    # Collect the pass result, detailed measurements, and preview paths.
    result = {"passed": all(checks.values()), "manifest": str(manifest_path), "checks": checks,
              "statistics": stats, "filter_measurements": measurements,
              "representative_signal_comparisons": comparisons, "previews": preview_paths,

              # Explain the coverage and assumptions behind this offline report.
              "limitations": ["RF numerical validation covers all exported samples; envelope comparisons use first/middle/last selected frames",
                              "Preview scan conversion assumes 1540 m/s and uses stored angles and coordinate grid",
                              "Supplied B-mode uses its own processing; pixel equality is not an acceptance criterion",
                              "No FPGA, DMA, DDSP or network behavior has been validated"]}

    # Save the report and release the disk-backed sample array.
    write_json(report_path, result)
    del words
    return result


# Run validation from the command line and report failure to the caller.
def main():

    # Choose the manifest, report destination, and preview option.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--no-previews", action="store_true")
    args = parser.parse_args()

    # Validate the package and print its checks.
    result = validate(args.manifest, args.report, not args.no_previews)
    print(json.dumps({"passed": result["passed"], "checks": result["checks"], "report": str(args.report)}, indent=2))

    # Return a failing exit status if any check failed.
    if not result["passed"]:
        raise SystemExit(1)


# Run the command only when this file is launched directly.
if __name__ == "__main__":
    main()
