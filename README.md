# miniC++

A small C++ → C translator written in Python.
Translate simple C++ code to C and optionally run it.

## What it looks like

```text
C++ file
   │
   ▼
 miniC++
   │
   ▼
Generated C
   │
   ▼
C Compiler
   │
   ▼
 Program
```

## Requirements

* Python 3
* C compiler

## Linux

### Debian / Ubuntu

```bash
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-

sudo apt update
sudo apt install python3 build-essential
```

Run:

```bash
python3 minicpp_full.py example.cpp --run
```

Other Linux distros only need **Python 3 + a C compiler**.

### Arch

```bash
sudo pacman -S python gcc
```

### Fedora

```bash
sudo dnf install python3 gcc
```

## Windows — CMD

Install **Python 3** and a C compiler such as **MinGW**.

```cmd
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-
```

Run:

```cmd
python minicpp_full.py example.cpp --run
```

## Windows — PowerShell

```powershell
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-
```

Run:

```powershell
python .\minicpp_full.py example.cpp --run
```

## Generate C

Linux:

```bash
python3 minicpp_full.py example.cpp -o output.c
```

Windows:

```cmd
python minicpp_full.py example.cpp -o output.c
```

## Usage

```text
python minicpp_full.py FILE.cpp [-o OUT.c] [--run] [--cc COMPILER]
```
