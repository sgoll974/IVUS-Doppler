"""Create the three verification figures selected for the symposium poster."""

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def load_analog(path):
    """Load completed analog trials and group them by commanded frequency."""
    groups = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            command = row.get("command_frequency_hz", "").strip()
            measured = row.get("measured_frequency_hz", "").strip()
            if not command or not measured:
                continue
            groups[float(command)].append(float(measured))
    if not groups:
        raise ValueError(f"No completed analog measurements were found in {path}")
    return groups


def load_network_csv(path):
    """Load elapsed time and interval throughput measurements."""
    elapsed = []
    wire_mbps = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            elapsed.append(float(row["elapsed_s"]))
            wire_mbps.append(float(row["interval_estimated_wire_mbps"]))
    if not elapsed:
        raise ValueError(f"No network time-series rows were found in {path}")
    return elapsed, wire_mbps


def analog_figure(plt, groups, output):
    commands = sorted(groups)
    means = [statistics.mean(groups[value]) for value in commands]
    deviations = [
        statistics.stdev(groups[value]) if len(groups[value]) > 1 else 0.0
        for value in commands
    ]
    errors = [
        abs(measured - command) / command * 100.0
        for command, measured in zip(commands, means)
    ]

    figure, axes = plt.subplots(2, 1, figsize=(7.2, 6.4), constrained_layout=True)
    lower = min(commands + means)
    upper = max(commands + means)
    axes[0].plot([lower, upper], [lower, upper], "--", color="#777777",
                 label="Ideal measured = commanded")
    axes[0].errorbar(commands, means, yerr=deviations, fmt="o-",
                     color="#0B3C5D", capsize=4, linewidth=2,
                     label="Red Pitaya measurement")
    axes[0].set_xscale("log")
    axes[0].set_yscale("log")
    axes[0].set_xlabel("Commanded frequency (Hz)")
    axes[0].set_ylabel("Measured frequency (Hz)")
    axes[0].set_title("Analog Input Frequency Verification")
    axes[0].grid(True, which="both", alpha=0.25)
    axes[0].legend()

    axes[1].semilogx(commands, errors, "o-", color="#D1495B", linewidth=2)
    axes[1].set_xlabel("Commanded frequency (Hz)")
    axes[1].set_ylabel("Absolute frequency error (%)")
    axes[1].grid(True, which="both", alpha=0.25)

    figure.savefig(output, dpi=220, bbox_inches="tight")
    plt.close(figure)


def integrity_figure(plt, summary, output):
    valid = int(summary["valid_frames"])
    damaged = int(summary["damaged_frames"])
    completion = float(summary["frame_completion_percent"])

    figure, axis = plt.subplots(figsize=(6.4, 5.2), constrained_layout=True)
    labels = ["CRC-valid frames", "Damaged frames"]
    values = [valid, damaged]
    colors = ["#2A9D8F", "#D1495B"]
    bars = axis.bar(labels, values, color=colors, width=0.58)
    axis.bar_label(bars, labels=[f"{value:,}" for value in values], padding=5,
                   fontsize=12, fontweight="bold")
    axis.set_ylabel("Frame count")
    axis.set_title(
        f"Five-Minute IVUS Stream Integrity\n"
        f"Frame completion = {completion:.3f}%"
    )
    axis.grid(axis="y", alpha=0.25)
    axis.text(
        0.5, -0.18,
        f"Missing packets: {summary['missing_packets']:,}   |   "
        f"CRC failures: {summary['crc_failures']:,}   |   "
        f"Malformed packets: {summary['malformed_packets']:,}",
        transform=axis.transAxes, ha="center", va="top", fontsize=9
    )
    maximum = max(values) if max(values) > 0 else 1
    axis.set_ylim(0, maximum * 1.15)
    figure.savefig(output, dpi=220, bbox_inches="tight")
    plt.close(figure)


def throughput_figure(plt, elapsed, wire_mbps, summary, output):
    theoretical = float(summary["theoretical_estimated_wire_mbps"])
    measured_mean = statistics.mean(wire_mbps)
    measured_std = statistics.stdev(wire_mbps) if len(wire_mbps) > 1 else 0.0

    figure, axis = plt.subplots(figsize=(7.4, 4.8), constrained_layout=True)
    axis.plot(elapsed, wire_mbps, color="#0B3C5D", linewidth=1.4,
              label="Measured estimated wire rate")
    axis.axhline(theoretical, color="#D1495B", linestyle="--", linewidth=2,
                 label=f"Theoretical = {theoretical:.2f} Mb/s")
    axis.axhline(measured_mean, color="#2A9D8F", linestyle=":", linewidth=2,
                 label=f"Measured mean = {measured_mean:.2f} Mb/s")
    axis.set_xlabel("Elapsed test time (s)")
    axis.set_ylabel("Estimated Ethernet rate (Mb/s)")
    axis.set_title(
        "End-to-End Ethernet Throughput\n"
        f"Mean {measured_mean:.2f} Mb/s, standard deviation {measured_std:.2f} Mb/s"
    )
    axis.grid(True, alpha=0.25)
    axis.legend(loc="best")
    finite_values = [value for value in wire_mbps + [theoretical] if math.isfinite(value)]
    if finite_values:
        axis.set_ylim(0, max(finite_values) * 1.18)
    figure.savefig(output, dpi=220, bbox_inches="tight")
    plt.close(figure)


def write_metrics(path, groups, summary, wire_mbps):
    """Write copy-ready values for the poster caption and report."""
    analog_errors = []
    for command, measurements in groups.items():
        mean_value = statistics.mean(measurements)
        analog_errors.append(abs(mean_value - command) / command * 100.0)

    lines = [
        "DOPPLER DYNAMICS V&V POSTER METRICS",
        "====================================",
        "",
        f"Analog frequencies tested: {len(groups)}",
        f"Maximum mean frequency error: {max(analog_errors):.6f}%",
        f"Mean frequency error: {statistics.mean(analog_errors):.6f}%",
        "",
        f"Valid frames: {summary['valid_frames']}",
        f"Damaged frames: {summary['damaged_frames']}",
        f"Frame completion: {summary['frame_completion_percent']:.6f}%",
        f"Missing packets: {summary['missing_packets']}",
        f"CRC failures: {summary['crc_failures']}",
        "",
        f"Measured mean estimated wire rate: {statistics.mean(wire_mbps):.6f} Mb/s",
        f"Measured wire-rate standard deviation: "
        f"{statistics.stdev(wire_mbps) if len(wire_mbps) > 1 else 0.0:.6f} Mb/s",
        f"Theoretical estimated wire rate: "
        f"{summary['theoretical_estimated_wire_mbps']:.6f} Mb/s",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analog", type=Path, required=True)
    parser.add_argument("--network-csv", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("poster_figures"))
    args = parser.parse_args()

    try:
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise SystemExit(
            "matplotlib is required. Install it with: py -m pip install matplotlib"
        ) from error

    groups = load_analog(args.analog)
    elapsed, wire_mbps = load_network_csv(args.network_csv)
    with args.summary.open("r", encoding="utf-8-sig") as stream:
        summary = json.load(stream)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    analog_output = args.output_dir / "01_analog_frequency_verification.png"
    integrity_output = args.output_dir / "02_frame_integrity.png"
    throughput_output = args.output_dir / "03_throughput_validation.png"
    metrics_output = args.output_dir / "poster_metrics.txt"

    analog_figure(plt, groups, analog_output)
    integrity_figure(plt, summary, integrity_output)
    throughput_figure(plt, elapsed, wire_mbps, summary, throughput_output)
    write_metrics(metrics_output, groups, summary, wire_mbps)

    print(f"Created {analog_output}")
    print(f"Created {integrity_output}")
    print(f"Created {throughput_output}")
    print(f"Created {metrics_output}")


if __name__ == "__main__":
    main()
