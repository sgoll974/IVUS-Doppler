"""Compile recorded RF into a header-free, paced IVUS playback package."""
import argparse
from pathlib import Path

import scipy
from ivus import (FORMAT, LIMIT, TARGET_FS, condition, h5py, make_filter, metadata,
                  np, quantize, read_frame, sha256, timing, write_json)


# Turn selected source frames into a binary and its playback manifest.
def compile_acquisition(source, output, start=0, stop=None, headroom=0.05, track=None, overwrite=False):

    # Resolve the source, final package, and temporary binary paths.
    source, output = Path(source).resolve(), Path(output).resolve()
    manifest_path = output.with_suffix(".json")
    temporary = output.with_suffix(output.suffix + ".partial")

    # Require a separate binary output so the source stays safe.
    if output.suffix != ".bin" or output == source:
        raise ValueError("Output must be a separate .bin file")

    # Protect existing packages unless replacement was requested.
    if not overwrite and any(p.exists() for p in (output, manifest_path, temporary)):
        raise FileExistsError("Output package already exists; use --overwrite deliberately")

    # Require a usable fraction of amplitude headroom.
    if not 0 <= headroom < 1:
        raise ValueError("Headroom must satisfy 0 <= headroom < 1")

    # Remember the source size and date before reading it twice.
    original_stat = source.stat()
    taps = make_filter()

    # Open the selected track and work out its playback timing.
    with h5py.File(source, "r") as file:
        meta = metadata(file, track)
        stop = meta["shape"][0] if stop is None else stop

        # Reject empty selections and frame numbers outside the source.
        if not 0 <= start < stop <= meta["shape"][0]:
            raise ValueError("Frame range must be nonempty and within the acquisition")
        pace = timing(meta, start, stop - start)

        # Start the first pass: find the largest signal before choosing one gain.
        peak = 0.0

        # Condition every selected frame using the same filter.
        for index in range(start, stop):
            values = condition(read_frame(file, meta, index), taps)
            peak = max(peak, float(np.max(np.abs(values))))

        # Choose one gain for the whole selection; leave silent input at zero.
        gain = LIMIT * (1 - headroom) / peak if peak else 0.0
        output.parent.mkdir(parents=True, exist_ok=True)

        # Start the second pass and collect the final sample statistics.
        minimum, maximum, max_mean, clips = LIMIT, -LIMIT, 0.0, 0

        # Write to a temporary binary until all checks pass.
        with temporary.open("wb") as stream:

            # Condition every selected frame using the same filter.
            for index in range(start, stop):
                values = condition(read_frame(file, meta, index), taps)

                # Round with the fixed gain and keep the original line order.
                words, count = quantize(values, gain)
                stream.write(words.tobytes(order="C"))

                # Track sample limits, line offsets, and any clipping.
                minimum, maximum = min(minimum, int(words.min())), max(maximum, int(words.max()))
                max_mean = max(max_mean, float(np.max(np.abs(words.mean(axis=-1)))))
                clips += count

                # Show progress at the first, last, and every tenth frame.
                if index == start or (index + 1 - start) % 10 == 0 or index == stop - 1:
                    print(f"Exported frame {index + 1}/{stop}", flush=True)

        # Check that the source stayed unchanged during both passes.
        final_stat = source.stat()

        # Reject results made while the source was being changed.
        if (original_stat.st_size, original_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise RuntimeError("Source changed during compilation; partial output retained")

        # Reject clipped samples or a line offset above half a count.
        if clips or max_mean > 0.5:
            raise RuntimeError("Quantization acceptance failed; partial output retained")

        # Require exactly two bytes for every exported sample.
        if temporary.stat().st_size != (stop - start) * meta["shape"][1] * (meta["shape"][2] // 8) * 2:
            raise RuntimeError("Output byte count is incorrect")
        # Remove the old manifest so an interrupted overwrite cannot look complete.
        if overwrite and manifest_path.exists():
            manifest_path.unlink()
        temporary.replace(output)

        # Copy the source settings for the selected frame range.
        selected_meta = dict(meta)

        # Keep only the probe positions belonging to the selected frames.
        if meta["pullback"]:
            selected_meta["pullback"] = dict(meta["pullback"], positions_m=meta["pullback"]["positions_m"][start:stop])

        # Describe the binary, its source, and how it was made.
        manifest = {
            "format": FORMAT, "modality": "IVUS", "transport": "unframed samples; DDSP integration pending source handoff",

            # Record the checksum, signed encoding, dimensions, and storage order.
            "binary": {"file": output.name, "bytes": output.stat().st_size, "sha256": sha256(output),
                       "dtype": "<i2", "encoding": "signed two's complement, right-aligned, sign-extended to 16 bits",
                       "range": [-LIMIT, LIMIT], "order": ["frame", "line", "axial_sample"],
                       "shape": [stop - start, meta["shape"][1], meta["shape"][2] // 8]},

            # Record the original file and the selected frame numbers.
            "source": {"path": str(source), "bytes": original_stat.st_size, "mtime_ns": original_stat.st_mtime_ns,
                       "frame_start": start, "frame_stop_exclusive": stop, "metadata": selected_meta},

            # Record the rate conversion and the filter limits.
            "conditioning": {"source_rate_hz": meta["source_sampling_frequency_hz"], "output_rate_hz": TARGET_FS,
                             "up": 1, "down": 8, "passband_edge_hz": 50_000_000,
                             "stopband_edge_hz": 62_500_000, "design_attenuation_db": 80,

                             # Keep the actual filter and explain padding and sample alignment.
                             "fir_taps": taps.tolist(), "fir_tap_count": len(taps),
                             "padding": "constant zero beyond each independent A-line",
                             "alignment": "symmetric odd FIR, delay compensated by resample_poly",

                             # Record offset removal, fixed gain, and the uncalibrated voltage scale.
                             "dc_policy": "subtract each resampled line's mean before fixed-gain quantization",
                             "headroom_fraction": headroom, "processed_peak": peak, "gain_adc_counts_per_source_unit": gain,
                             "voltage_mapping": "simulated full scale; source RF is not calibrated in volts"},

            # Keep playback timing separate from stored sample values.
            "timing": pace,

            # Record the final sample limits and clipping count.
            "export_statistics": {"minimum": minimum, "maximum": maximum,
                                  "maximum_absolute_line_mean_counts": max_mean, "clipped_samples": clips},

            # Record library versions so the processing can be reproduced.
            "software": {"numpy": np.__version__, "scipy": scipy.__version__, "h5py": h5py.__version__},

            # Keep the source credit and describe the dataset changes.
            "license": {"name": "CC BY 4.0", "credit": meta["credit"],
                        "modifications": "1:8 anti-alias resampling, per-line DC removal, fixed gain and signed quantization"}}

        # Publish the manifest after the final binary is in place.
        write_json(manifest_path, manifest)
    return manifest_path


# Read export options and create the requested playback package.
def main():

    # Choose source and output files and an optional frame range.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-start", type=int, default=0)
    parser.add_argument("--frame-stop", type=int, help="Exclusive; defaults to acquisition end")

    # Choose scaling, track selection, and whether replacement is allowed.
    parser.add_argument("--headroom", type=float, default=0.05)
    parser.add_argument("--track")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    # Compile the package and print the manifest location.
    path = compile_acquisition(args.input, args.output, args.frame_start, args.frame_stop,
                               args.headroom, args.track, args.overwrite)
    print(f"Manifest: {path}")


# Run the command only when this file is launched directly.
if __name__ == "__main__":
    main()
