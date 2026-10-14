"""Prepare and verify every acquisition; stop at the first failed artifact."""
import argparse
import json
from pathlib import Path

from ivus import write_json
from preprocess_openhrf import compile_acquisition
from validate_openhrf import validate


# Write a readable summary after the whole batch passes.
def write_summary_markdown(output, summary):

    # Do not publish a completion report for an unfinished batch.
    if not summary["passed"]:
        raise ValueError("Cannot publish a completion report for an incomplete batch")

    # Start the report with totals and the acquisition table.
    lines = ["# Offline IVUS validation", "",
             f"Prepared and verified {len(summary['acquisitions'])} complete acquisitions, "
             f"{sum(row['frames'] for row in summary['acquisitions'])} frames, "
             f"and {summary['total_bytes']:,} sample bytes.", "",

             # Explain the sample encoding and how all samples were checked.
             "Samples are 125 MSPS, signed 14-bit values in little-endian int16 containers. "
             "All exported samples were compared with the conditioned source RF. "
             "Each acquisition uses one fixed gain and 5% amplitude headroom.", "",

             # Label the per-acquisition results table.
             "| Acquisition | Frames | Lines/frame | Payload MiB | Max line DC, counts | Min envelope correlation |",
             "|---|---:|---:|---:|---:|---:|"]

    # Add the payload size and fidelity results for each acquisition.
    for row in summary["acquisitions"]:
        lines.append(f"| [{row['acquisition']}]({row['acquisition']}/validation.json) | {row['frames']} | "
                     f"{row['shape'][1]} | {row['bytes']/1024**2:.2f} | "
                     f"{row['maximum_line_dc_counts']:.6f} | {row['minimum_envelope_correlation']:.6f} |")

    # Explain the numerical limits required for acceptance.
    lines.extend(["", "Acceptance requires no clipping, range within [-8191, 8191], "
                  "line DC residual <=0.5 count, quantization error <=0.5 count, "
                  "FIR passband deviation <=0.02 dB, stopband attenuation >=70 dB, "
                  "and representative native/exported envelope correlation >=0.99.", "",

                  # Explain the limited frame coverage and image assumptions.
                  "Envelope comparisons use the first, middle, and last frame of each acquisition. "
                  "Scan-conversion previews use stored coordinates and angles with assumed sound speed "
                  "1540 m/s. The supplied B-mode has different display processing.", "",

                  # List the package files and the separate single-frame example.
                  "Each acquisition folder contains its binary, manifest, validation JSON, and three "
                  "comparison images. The one_frame folder provides a separate 720 KiB fixture.", "",

                  # State which hardware work still needs to be validated.
                  "This is offline verification. FPGA injection, DDR reservations, DMA, DDSP, "
                  "simultaneous Doppler acquisition, network delivery, and GUI integration still "
                  "require hardware validation and Micaela's tested source package.", "",

                  # Keep the dataset credit and describe the processing changes.
                  "Source attribution: Mosaic Intelligence - NuevoSono IVUS porcine dataset (CC BY 4.0). "
                  "Modifications include resampling, DC removal, scaling, and quantization.", ""])

    # Save the completed Markdown report.
    (Path(output) / "validation_summary.md").write_text("\n".join(lines), encoding="utf-8")


# Prepare every acquisition and stop if any package fails validation.
def main():

    # Choose the source directory, output directory, and resume options.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Directory of HDF5 acquisitions")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Revalidate existing complete packages instead of overwriting")
    parser.add_argument("--no-previews", action="store_true")
    args = parser.parse_args()

    # Process acquisitions in a consistent filename order.
    paths = sorted(args.input.glob("*.hdf5"))

    # Stop if no source acquisitions were found.
    if not paths:
        parser.error("No acquisitions found")

    # Mark the batch unfinished before processing any files.
    summary = {"passed": False, "acquisitions": [], "input": str(args.input.resolve())}
    write_json(args.output / "summary.json", summary)
    total_bytes = 0

    # Give each acquisition its own binary, manifest, and report folder.
    for path in paths:
        print(f"PREPARING {path.stem}", flush=True)
        folder = args.output / path.stem
        binary = folder / "ivus_timescale_stream.bin"
        manifest = binary.with_suffix(".json")

        # Reuse an existing manifest only when resume was requested.
        if not args.resume or not manifest.exists():
            compile_acquisition(path, binary)

        # Read the package settings before accepting a resumed result.
        package = json.loads(manifest.read_text(encoding="utf-8"))

        # Reject a different source or a package containing only some frames.
        if (Path(package["source"]["path"]).resolve() != path.resolve()
                or package["source"]["frame_start"] != 0
                or package["source"]["frame_stop_exclusive"] != package["source"]["metadata"]["shape"][0]):
            raise ValueError(f"Resume package is not the complete requested acquisition: {manifest}")

        # Validate every package again, including packages reused on resume.
        result = validate(manifest, folder / "validation.json", not args.no_previews)

        # Stop the batch as soon as an acquisition fails.
        if not result["passed"]:
            raise RuntimeError(f"Validation failed: {folder / 'validation.json'}")

        # Add the verified acquisition to the running batch totals.
        total_bytes += binary.stat().st_size
        summary["acquisitions"].append({"acquisition": path.stem, "frames": package["binary"]["shape"][0],
            "shape": package["binary"]["shape"], "bytes": binary.stat().st_size,

            # Link the package reports and retain their sample measurements.
            "manifest": str(manifest.resolve()), "report": str((folder / "validation.json").resolve()),
            "minimum": result["statistics"]["minimum"], "maximum": result["statistics"]["maximum"],
            "maximum_line_dc_counts": result["statistics"]["maximum_absolute_line_mean_counts"],
            "minimum_envelope_correlation": min((item["envelope_correlation"] for item in result["representative_signal_comparisons"]), default=None)})

        # Save progress while keeping the batch marked unfinished.
        summary["total_bytes"] = total_bytes
        write_json(args.output / "summary.json", summary)
        print(f"PASSED {path.stem}: {binary.stat().st_size / (1024**2):.2f} MiB", flush=True)

    # Publish completion only after every acquisition has passed.
    summary["passed"] = True
    write_json(args.output / "summary.json", summary)
    write_summary_markdown(args.output, summary)
    print(f"COMPLETE: {len(paths)} acquisitions, {total_bytes:,} sample bytes", flush=True)


# Run the command only when this file is launched directly.
if __name__ == "__main__":
    main()
