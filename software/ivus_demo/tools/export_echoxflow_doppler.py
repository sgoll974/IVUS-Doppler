#!/usr/bin/env python3
"""Export one EchoXFlow LVOT Doppler examination for the DD demo.

The script reads the two Zarr recordings selected for the demonstration:

* recording_7bbc7f52a98c20a0: pulsed-wave LVOT Doppler, ECG, and traces
* recording_68dea4212b32ea6f: LVOT diameter measurement

It calculates beat-level VTI, stroke volume, and cardiac output, then writes a
small compressed NPZ file, a JSON result summary, and a PNG validation plot.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import zlib
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import zarr


DEFAULT_EXAM_ID = "exam_40a7fcbd73d4ae1b"
DEFAULT_DOPPLER_RECORDING = "recording_7bbc7f52a98c20a0"
DEFAULT_DIAMETER_RECORDING = "recording_68dea4212b32ea6f"
DDP_MAGIC = b"DDP1"
DDP_VERSION = 1
DDP_FLAG_BEAT_UPDATE = 0x00000001
DDP_HEADER = struct.Struct("<4s11I")
DDP_SAMPLE_HEADER = struct.Struct("<7I")


def parse_args() -> argparse.Namespace:
    """Read command-line paths and optional recording identifiers."""
    parser = argparse.ArgumentParser(
        description=(
            "Export the selected EchoXFlow LVOT Doppler examination into a "
            "compact Doppler Dynamics demonstration package."
        )
    )
    parser.add_argument(
        "--exam-dir",
        required=True,
        type=Path,
        help="Directory containing the examination's .zarr recording folders",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory in which the NPZ, JSON, and PNG outputs are written",
    )
    parser.add_argument(
        "--doppler-recording",
        default=DEFAULT_DOPPLER_RECORDING,
        help="Recording ID containing pulsed-wave LVOT Doppler",
    )
    parser.add_argument(
        "--diameter-recording",
        default=DEFAULT_DIAMETER_RECORDING,
        help="Recording ID containing the LVOT diameter caliper",
    )
    return parser.parse_args()


def open_recording(exam_dir: Path, recording_id: str) -> zarr.hierarchy.Group:
    """Open one EchoXFlow Zarr recording after validating its location."""
    recording_path = exam_dir / f"{recording_id}.zarr"
    if not recording_path.is_dir():
        raise FileNotFoundError(f"Recording directory not found: {recording_path}")
    return zarr.open_group(str(recording_path), mode="r")


def measurement_annotations(group: zarr.hierarchy.Group) -> list[dict[str, Any]]:
    """Return only measurement annotations from a recording manifest."""
    manifest = group.attrs.get("recording_manifest", {})
    annotations = manifest.get("annotations", [])
    return [item for item in annotations if item.get("kind") == "measurement"]


def find_measurements(
    group: zarr.hierarchy.Group, measurement_kind: str
) -> list[np.ndarray]:
    """Load every annotation array matching a measurement-kind identifier."""
    matches: list[np.ndarray] = []
    for annotation in measurement_annotations(group):
        links = annotation.get("links", {})
        if links.get("measurement_kind") != measurement_kind:
            continue
        value_path = annotation["value"]["zarr_path"]
        matches.append(np.asarray(group[value_path], dtype=np.float64))
    if not matches:
        raise ValueError(f"No '{measurement_kind}' annotation was found")
    return matches


def calculate_lvot_diameter_cm(diameter_group: zarr.hierarchy.Group) -> float:
    """Calculate LVOT diameter from the two physical caliper coordinates."""
    points = find_measurements(diameter_group, "dimension_lvot_diam")[0]
    if points.shape != (2, 2):
        raise ValueError(f"Expected two LVOT caliper points; received {points.shape}")
    diameter_m = float(np.linalg.norm(points[1] - points[0]))
    return diameter_m * 100.0


def integrate_vti_cm(trace_points: np.ndarray) -> dict[str, float]:
    """Integrate one LVOT velocity envelope and return beat measurements.

    Annotation columns are time in seconds and velocity in metres per second.
    The first and last points define the local zero-velocity baseline. The area
    under the baseline-corrected absolute envelope is metres per beat, which is
    converted to centimetres per beat for the conventional VTI calculation.
    """
    if trace_points.ndim != 2 or trace_points.shape[1] != 2:
        raise ValueError(f"Expected an N x 2 Doppler trace; received {trace_points.shape}")

    time_s = trace_points[:, 0]
    velocity_mps = trace_points[:, 1]
    if np.any(np.diff(time_s) < 0):
        raise ValueError("Doppler trace timestamps are not monotonic")

    baseline_mps = float((velocity_mps[0] + velocity_mps[-1]) / 2.0)
    corrected_velocity = np.abs(velocity_mps - baseline_mps)
    try:
        trapezoid = np.trapezoid
    except AttributeError:  # NumPy versions older than 2.0
        trapezoid = np.trapz
    vti_cm = float(trapezoid(corrected_velocity, time_s) * 100.0)

    return {
        "start_time_s": float(time_s[0]),
        "end_time_s": float(time_s[-1]),
        "duration_s": float(time_s[-1] - time_s[0]),
        "peak_velocity_mps": float(np.max(corrected_velocity)),
        "baseline_velocity_mps": baseline_mps,
        "vti_cm": vti_cm,
    }


def velocity_axis_mps(
    spectral_bins: int, nyquist_mps: float, baseline_fraction: float
) -> np.ndarray:
    """Construct the displayed velocity coordinate for the spectral bins."""
    top_velocity = baseline_fraction * nyquist_mps
    bottom_velocity = -(1.0 - baseline_fraction) * nyquist_mps
    return np.linspace(top_velocity, bottom_velocity, spectral_bins, dtype=np.float32)


def create_preview(
    output_path: Path,
    spectrum: np.ndarray,
    doppler_time_s: np.ndarray,
    velocity_axis: np.ndarray,
    ecg: np.ndarray,
    ecg_time_s: np.ndarray,
    traces: list[np.ndarray],
    metrics: dict[str, Any],
) -> None:
    """Create a validation figure containing Doppler, ECG, and calculations."""
    figure, axes = plt.subplots(
        3,
        1,
        figsize=(13, 9),
        gridspec_kw={"height_ratios": [5, 1.3, 1.4]},
        constrained_layout=True,
    )

    time_origin = float(doppler_time_s[0])
    axes[0].imshow(
        spectrum.T,
        aspect="auto",
        origin="upper",
        cmap="gray",
        extent=[
            float(doppler_time_s[0] - time_origin),
            float(doppler_time_s[-1] - time_origin),
            float(velocity_axis[-1]),
            float(velocity_axis[0]),
        ],
        interpolation="nearest",
    )
    for index, trace in enumerate(traces, start=1):
        axes[0].plot(
            trace[:, 0] - time_origin,
            trace[:, 1],
            linewidth=2.0,
            label=f"Manual LVOT trace {index}",
        )
    axes[0].axhline(0.0, color="#35d07f", linewidth=1.0, alpha=0.9)
    axes[0].set_title("EchoXFlow Pulsed-Wave LVOT Doppler")
    axes[0].set_xlabel("Time (s)")
    axes[0].set_ylabel("Velocity (m/s)")
    axes[0].legend(loc="lower right", fontsize=8)

    axes[1].plot(ecg_time_s - time_origin, ecg, color="#d14b64", linewidth=0.8)
    axes[1].set_xlim(
        float(doppler_time_s[0] - time_origin),
        float(doppler_time_s[-1] - time_origin),
    )
    axes[1].set_title("Synchronized ECG")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_ylabel("Amplitude")
    axes[1].grid(alpha=0.2)

    axes[2].axis("off")
    summary_text = (
        f"LVOT diameter: {metrics['lvot_diameter_cm']:.3f} cm     "
        f"LVOT area: {metrics['lvot_area_cm2']:.3f} cm²     "
        f"Heart rate: {metrics['heart_rate_bpm']:.1f} bpm\n"
        f"Mean VTI: {metrics['mean_vti_cm']:.3f} cm     "
        f"Mean stroke volume: {metrics['mean_stroke_volume_ml']:.3f} mL     "
        f"Mean cardiac output: {metrics['mean_cardiac_output_lpm']:.3f} L/min"
    )
    axes[2].text(
        0.5,
        0.55,
        summary_text,
        ha="center",
        va="center",
        fontsize=14,
        bbox={"boxstyle": "round,pad=0.7", "facecolor": "#eaf2f8", "edgecolor": "#1f4e78"},
    )
    figure.suptitle("Doppler Dynamics — EchoXFlow Export Validation", fontsize=17)
    figure.savefig(output_path, dpi=180)
    plt.close(figure)


def write_ddp_file(
    output_path: Path,
    spectrum: np.ndarray,
    doppler_time_s: np.ndarray,
    heart_rate_bpm: float,
    diameter_cm: float,
    area_cm2: float,
    beat_results: list[dict[str, float]],
    mean_vti_cm: float,
    mean_sv_ml: float,
    mean_co_lpm: float,
) -> None:
    """Write the compact binary file consumed by the Red Pitaya C sender."""
    relative_time_s = doppler_time_s - doppler_time_s[0]
    sample_rate_hz = 1.0 / float(np.median(np.diff(doppler_time_s)))
    rate_num = int(round(sample_rate_hz * 1000.0))
    rate_den = 1000

    file_header = DDP_HEADER.pack(
        DDP_MAGIC,
        DDP_VERSION,
        int(spectrum.shape[0]),
        int(spectrum.shape[1]),
        rate_num,
        rate_den,
        int(round(heart_rate_bpm * 1000.0)),
        int(round(diameter_cm * 10000.0)),
        int(round(area_cm2 * 100000.0)),
        int(round(mean_vti_cm * 1000.0)),
        int(round(mean_sv_ml * 1000.0)),
        int(round(mean_co_lpm * 1000.0)),
    )

    active_vti = 0.0
    active_sv = 0.0
    active_co = 0.0
    next_beat = 0

    with output_path.open("wb") as stream:
        stream.write(file_header)
        for sample_index, (time_s, spectral_line) in enumerate(
            zip(relative_time_s, spectrum, strict=True)
        ):
            flags = 0
            absolute_time_s = float(time_s + doppler_time_s[0])
            while (
                next_beat < len(beat_results)
                and absolute_time_s >= beat_results[next_beat]["end_time_s"]
            ):
                active_vti = beat_results[next_beat]["vti_cm"]
                active_sv = beat_results[next_beat]["stroke_volume_ml"]
                active_co = beat_results[next_beat]["cardiac_output_lpm"]
                flags |= DDP_FLAG_BEAT_UPDATE
                next_beat += 1

            line_bytes = np.asarray(spectral_line, dtype=np.uint8).tobytes()
            line_crc = zlib.crc32(line_bytes) & 0xFFFFFFFF
            sample_header = DDP_SAMPLE_HEADER.pack(
                sample_index,
                int(round(float(time_s) * 1_000_000.0)),
                int(round(active_vti * 1000.0)),
                int(round(active_sv * 1000.0)),
                int(round(active_co * 1000.0)),
                line_crc,
                flags,
            )
            stream.write(sample_header)
            stream.write(line_bytes)


def main() -> None:
    """Run extraction, calculation, compact-file export, and visual validation."""
    args = parse_args()
    exam_dir = args.exam_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    doppler_group = open_recording(exam_dir, args.doppler_recording)
    diameter_group = open_recording(exam_dir, args.diameter_recording)

    spectrum = np.asarray(
        doppler_group["data/1d_pulsed_wave_doppler"], dtype=np.uint8
    )
    doppler_time_s = np.asarray(
        doppler_group["timestamps/1d_pulsed_wave_doppler"], dtype=np.float64
    )
    ecg = np.asarray(doppler_group["data/ecg"], dtype=np.float32)
    ecg_time_s = np.asarray(doppler_group["timestamps/ecg"], dtype=np.float64)
    nyquist_series = np.asarray(
        doppler_group["coordinates/pw_doppler_nyquist_mps"], dtype=np.float64
    )
    baseline_series = np.asarray(
        doppler_group["coordinates/pw_doppler_baseline_fraction"], dtype=np.float64
    )

    traces = sorted(
        find_measurements(doppler_group, "aortic_lvot_trace_manual"),
        key=lambda trace: float(trace[0, 0]),
    )
    heart_rate_bpm = float(
        doppler_group.attrs["recording_manifest"]["metadata"]["heart_rate_bpm"]
    )
    diameter_cm = calculate_lvot_diameter_cm(diameter_group)
    area_cm2 = math.pi * (diameter_cm / 2.0) ** 2

    beat_results: list[dict[str, float]] = []
    for beat_number, trace in enumerate(traces, start=1):
        beat = integrate_vti_cm(trace)
        beat["beat_number"] = beat_number
        beat["stroke_volume_ml"] = area_cm2 * beat["vti_cm"]
        beat["cardiac_output_lpm"] = (
            beat["stroke_volume_ml"] * heart_rate_bpm / 1000.0
        )
        beat_results.append(beat)

    mean_vti_cm = float(np.mean([beat["vti_cm"] for beat in beat_results]))
    mean_sv_ml = area_cm2 * mean_vti_cm
    mean_co_lpm = mean_sv_ml * heart_rate_bpm / 1000.0

    nyquist_mps = float(np.median(nyquist_series))
    baseline_fraction = float(np.median(baseline_series))
    velocity_axis = velocity_axis_mps(
        spectrum.shape[1], nyquist_mps, baseline_fraction
    )

    metrics: dict[str, Any] = {
        "dataset": "Ahus-AIM/EchoXFlow",
        "license": "CC BY-NC-SA 4.0",
        "exam_id": DEFAULT_EXAM_ID,
        "doppler_recording_id": args.doppler_recording,
        "diameter_recording_id": args.diameter_recording,
        "source_disclosure": (
            "Prerecorded retrospective clinical Doppler examination; not live "
            "probe acquisition and not patient-matched to the IVUS demo dataset."
        ),
        "heart_rate_bpm": heart_rate_bpm,
        "lvot_diameter_cm": diameter_cm,
        "lvot_area_cm2": area_cm2,
        "mean_vti_cm": mean_vti_cm,
        "mean_stroke_volume_ml": mean_sv_ml,
        "mean_cardiac_output_lpm": mean_co_lpm,
        "doppler_samples": int(spectrum.shape[0]),
        "spectral_bins": int(spectrum.shape[1]),
        "duration_s": float(doppler_time_s[-1] - doppler_time_s[0]),
        "median_sample_rate_hz": float(1.0 / np.median(np.diff(doppler_time_s))),
        "nyquist_velocity_mps": nyquist_mps,
        "baseline_fraction": baseline_fraction,
        "beats": beat_results,
        "equations": {
            "lvot_area": "pi * (diameter_cm / 2)^2",
            "stroke_volume": "lvot_area_cm2 * vti_cm",
            "cardiac_output": "stroke_volume_ml * heart_rate_bpm / 1000",
        },
    }

    npz_path = output_dir / "echoxflow_lvot_demo.npz"
    json_path = output_dir / "echoxflow_lvot_summary.json"
    preview_path = output_dir / "echoxflow_lvot_preview.png"
    ddp_path = output_dir / "echoxflow_lvot_demo.ddp"

    trace_time_offsets = np.array(
        [trace[0, 0] - doppler_time_s[0] for trace in traces], dtype=np.float64
    )
    np.savez_compressed(
        npz_path,
        spectrum=spectrum,
        doppler_time_s=doppler_time_s - doppler_time_s[0],
        velocity_axis_mps=velocity_axis,
        ecg=ecg,
        ecg_time_s=ecg_time_s - doppler_time_s[0],
        trace_time_offsets_s=trace_time_offsets,
        heart_rate_bpm=np.float32(heart_rate_bpm),
        lvot_diameter_cm=np.float32(diameter_cm),
        lvot_area_cm2=np.float32(area_cm2),
        mean_vti_cm=np.float32(mean_vti_cm),
        mean_stroke_volume_ml=np.float32(mean_sv_ml),
        mean_cardiac_output_lpm=np.float32(mean_co_lpm),
    )
    json_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    write_ddp_file(
        ddp_path,
        spectrum,
        doppler_time_s,
        heart_rate_bpm,
        diameter_cm,
        area_cm2,
        beat_results,
        mean_vti_cm,
        mean_sv_ml,
        mean_co_lpm,
    )
    create_preview(
        preview_path,
        spectrum,
        doppler_time_s,
        velocity_axis,
        ecg,
        ecg_time_s,
        traces,
        metrics,
    )

    print(f"Created {npz_path}")
    print(f"Created {json_path}")
    print(f"Created {preview_path}")
    print(f"Created {ddp_path}")
    print("\nCalculated demonstration values")
    print(f"LVOT diameter: {diameter_cm:.3f} cm")
    print(f"Heart rate: {heart_rate_bpm:.1f} bpm")
    for beat in beat_results:
        print(
            f"Beat {beat['beat_number']}: VTI={beat['vti_cm']:.3f} cm, "
            f"SV={beat['stroke_volume_ml']:.3f} mL, "
            f"CO={beat['cardiac_output_lpm']:.3f} L/min"
        )
    print(f"Mean cardiac output: {mean_co_lpm:.3f} L/min")


if __name__ == "__main__":
    main()
