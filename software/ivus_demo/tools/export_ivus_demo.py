"""Export supplied IVUS B-mode frames and lumen areas into a compact demo file."""

import argparse
import struct
import sys
import zlib
from fractions import Fraction
from pathlib import Path


FILE_MAGIC = b"DDF1"
FILE_VERSION = 1
FILE_HEADER = struct.Struct("<4s7I")
FRAME_HEADER = struct.Struct("<4I")


def decode_label(value):
    """Return an HDF5 label as normal text."""
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def pixel_area_mm2(coordinates, np):
    """Estimate Cartesian pixel area from neighboring stored coordinates."""
    horizontal = np.linalg.norm(np.diff(coordinates, axis=1), axis=-1)
    vertical = np.linalg.norm(np.diff(coordinates, axis=0), axis=-1)
    dx = float(np.median(horizontal[horizontal > 0]))
    dy = float(np.median(vertical[vertical > 0]))
    if not np.isfinite(dx) or not np.isfinite(dy) or dx <= 0 or dy <= 0:
        raise ValueError("Could not determine a positive image-pixel area")
    return dx * dy * 1_000_000.0


def export(source, output, repo_scripts, start, stop):
    """Write selected supplied B-mode frames and lumen areas to one DDF file."""
    sys.path.insert(0, str(repo_scripts.resolve()))
    from ivus import h5py, metadata, np  # Uses the team's existing schema checks.

    source = source.resolve()
    output = output.resolve()
    with h5py.File(source, "r") as file:
        meta = metadata(file)
        data_path = meta["data_path"]
        images = file[data_path + "image/values"]
        segmentations = file[data_path + "segmentation/values"]
        labels = [decode_label(x) for x in file[data_path + "segmentation/labels"][()]]
        if "lumen" not in labels:
            raise ValueError(f"No lumen segmentation in labels: {labels}")
        lumen_index = labels.index("lumen")
        coordinates = np.asarray(file[data_path + "image/coordinates"][()], dtype=np.float64)

        if len(images.shape) != 3 or images.shape[0] != meta["shape"][0]:
            raise ValueError(f"Unexpected supplied image shape: {images.shape}")
        if segmentations.shape[:3] != images.shape:
            raise ValueError("Image and segmentation dimensions do not match")

        frame_total, height, width = map(int, images.shape)
        stop = frame_total if stop is None else stop
        if not 0 <= start < stop <= frame_total:
            raise ValueError("Frame range must be nonempty and inside the acquisition")

        rate = Fraction(meta["frame_rate_hz"]).limit_denominator(1_000_000)
        area_per_pixel = pixel_area_mm2(coordinates, np)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".partial")

        with temporary.open("wb") as stream:
            stream.write(FILE_HEADER.pack(
                FILE_MAGIC, FILE_VERSION, stop - start, width, height,
                rate.numerator, rate.denominator, 0
            ))
            for local_index, source_index in enumerate(range(start, stop)):
                pixels = np.asarray(images[source_index], dtype=np.uint8, order="C").tobytes()
                lumen = np.asarray(segmentations[source_index, :, :, lumen_index], dtype=bool)
                area_milli = int(round(float(np.count_nonzero(lumen)) * area_per_pixel * 1000.0))
                crc = zlib.crc32(pixels) & 0xFFFFFFFF
                stream.write(FRAME_HEADER.pack(local_index, area_milli, len(pixels), crc))
                stream.write(pixels)
                print(
                    f"Exported frame {source_index}: {width}x{height}, "
                    f"lumen area {area_milli / 1000.0:.3f} mm^2",
                    flush=True,
                )
        temporary.replace(output)

    print(f"Created {output}")
    print(f"Frames: {stop - start}; playback rate: {float(rate):.6g} fps")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="15_10_50_19.hdf5")
    parser.add_argument("--output", type=Path, required=True, help="Output .ddf file")
    parser.add_argument("--repo-scripts", type=Path, default=Path("scripts"),
                        help="Directory containing the repository's ivus.py")
    parser.add_argument("--frame-start", type=int, default=0)
    parser.add_argument("--frame-stop", type=int)
    args = parser.parse_args()
    export(args.input, args.output, args.repo_scripts, args.frame_start, args.frame_stop)


if __name__ == "__main__":
    main()
