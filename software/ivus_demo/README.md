# Doppler Dynamics IVUS network demo

This package adds a repeatable demonstration path without modifying the original
`/root/clean` GUI:

`15_10_50_19.hdf5 -> DDF demo file -> Red Pitaya ARM UDP sender -> Raspberry Pi GUI`

The first demo mode transports the dataset's supplied B-mode frames and a lumen
area calculated from the supplied segmentation. It does **not** claim live IVUS
acquisition, FPGA DMA validation, or measured Doppler velocity.

If you received this as `Doppler_Dynamics_IVUS_Demo.tar.gz`, extract it in
PowerShell before following the sections below:

```powershell
Set-Location "$HOME\Downloads"
tar -xzf ".\Doppler_Dynamics_IVUS_Demo.tar.gz"
```

## 1. Prepare the selected acquisition on Windows

Run these commands from the cloned `IVUS-Doppler` repository. Create the Python
environment and install both the processing dependencies and official Hugging
Face downloader:

```powershell
py -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install numpy scipy h5py hdf5plugin matplotlib huggingface_hub
& ".\.venv\Scripts\hf.exe" download nvidia/Open-H-RF `
  "mosaic-nuevosono/data/15_10_50_19.hdf5" `
  --repo-type dataset --local-dir ".\data\source_download"
$dataset = ".\data\source_download\mosaic-nuevosono\data\15_10_50_19.hdf5"
```

First use the existing repository scripts to inspect, condition, and validate a
10-frame RF package:

```powershell
New-Item -ItemType Directory -Force -Path ".\data\prepared\demo10"
& ".\.venv\Scripts\python.exe" ".\scripts\inspect_openhrf.py" `
  $dataset `
  --output ".\data\prepared\inspection.json"
& ".\.venv\Scripts\python.exe" ".\scripts\preprocess_openhrf.py" `
  $dataset `
  --output ".\data\prepared\demo10\ivus_timescale_stream.bin" `
  --frame-stop 10
& ".\.venv\Scripts\python.exe" ".\scripts\validate_openhrf.py" `
  ".\data\prepared\demo10\ivus_timescale_stream.json" `
  --report ".\data\prepared\demo10\validation.json"
```

Copy `tools/export_ivus_demo.py` from this package into the repository root, or
refer to its full path. Export all 60 supplied B-mode frames for the reliable
GUI demonstration:

```powershell
& ".\.venv\Scripts\python.exe" ".\export_ivus_demo.py" `
  $dataset `
  --repo-scripts ".\scripts" `
  --output ".\data\prepared\15_10_50_19_demo.ddf"
```

The `.ddf` file contains only the 60 selected acquisition frames, their lumen
areas, playback rate, dimensions, and per-frame CRC32 values.

The acquisition is the Mosaic Intelligence / NuevoSono in-vivo porcine IVUS
dataset distributed by NVIDIA Open-H-RF under CC BY 4.0. Retain that attribution
on the demo slide and in derivative materials.

## 2. Install the package on the Raspberry Pi

Copy the complete `dd_ivus_demo` directory to the separate account:

```powershell
scp -O -r .\dd_ivus_demo micaela@192.168.10.2:/home/micaela/
```

On the Pi:

```bash
cd ~/dd_ivus_demo/gui
make clean
make
```

Do not replace `/root/clean` or its service. The new executable is
`~/dd_ivus_demo/gui/gui_stream`.

## 3. Install the sender on the Red Pitaya

Copy the complete package so the relative `protocol.h` include stays intact:

```powershell
scp -O -r .\dd_ivus_demo root@RED_PITAYA_IP:/root/
scp -O ".\data\prepared\15_10_50_19_demo.ddf" `
  root@RED_PITAYA_IP:/root/dd_ivus_demo/
```

On the Red Pitaya:

```bash
cd /root/dd_ivus_demo/red_pitaya
make clean
make
```

This sender reads a file from Linux storage on the Red Pitaya ARM. It does not
read FPGA DMA memory.

## 4. Validate UDP before launching the GUI

On the Pi:

```bash
cd ~/dd_ivus_demo
python3 tools/dd_udp_receiver_test.py --output received_frame.pgm
```

While it is listening, run on the Red Pitaya:

```bash
cd /root/dd_ivus_demo/red_pitaya
./dd_udp_sender ../15_10_50_19_demo.ddf 192.168.10.2 8080 1
```

The receiver must report one complete frame, a valid CRC32, dimensions, and a
lumen area. This isolates network and packet problems from GUI problems.

## 5. Run the integrated GUI

The original GUI owns the display, so stop it only when ready to test. Keep the
original files and service unchanged.

```bash
sudo systemctl stop gui.service
cd ~/dd_ivus_demo/gui
sudo ./gui_stream
```

In another Red Pitaya SSH terminal, loop the acquisition continuously:

```bash
cd /root/dd_ivus_demo/red_pitaya
./dd_udp_sender ../15_10_50_19_demo.ddf 192.168.10.2 8080 0
```

`0` means repeat until `Ctrl+C`. The GUI listens on UDP port 8080.

To return to the original display:

```bash
sudo systemctl start gui.service
```

## Demo claims that are supported

- Controlled function-generator testing verified the Red Pitaya analog ADC,
  FPGA oscilloscope, and Ethernet visualization path.
- A selected recorded porcine IVUS acquisition was inspected and conditioned
  with the repository scripts.
- Recorded B-mode frames were packetized on the Red Pitaya ARM and transported
  physically over UDP/Ethernet to the Raspberry Pi.
- The Pi reassembled frames, checked CRC32, tracked packet/frame errors, and
  displayed dataset-derived lumen area.

## Claims that are not yet supported

- Live IVUS acquisition through the Red Pitaya FPGA/DMA path
- A measured signal from the Parks Doppler probe
- Measured velocity, volumetric flow, or cardiac output
- Clinical accuracy or suitability for patient use

