# miniC++ Studio

A lightweight C++ IDE and C translator built with Python and Tkinter.
Write, run, edit, and view generated C code in one simple interface.

## Features

* C++ code editor
* Run with **F5**
* Standard input and output
* Open / Save `.cpp` files
* View generated C code
* Automatic C compiler detection

## Install

### Linux — Debian / Ubuntu

```bash
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-

sudo apt update
sudo apt install python3 python3-tk build-essential
```

Run:

```bash
python3 minicpp_full.py
```

### Arch Linux

```bash
sudo pacman -S python tk gcc
```

### Fedora

```bash
sudo dnf install python3 python3-tk gcc
```

### Windows — CMD

Install **Python 3** and a C compiler such as **MinGW**.

```cmd
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-
python minicpp_full.py
```

### Windows — PowerShell

```powershell
git clone https://github.com/holyyyyyyyyyyyyyyyyyyyyyyyyy/minic-usingpython-.git
cd minic-usingpython-
python .\minicpp_full.py
```

## Requirements

* Python 3
* Tkinter
* C compiler: `cc`, `gcc`, `clang`, or `tcc`

## How It Works

```text
C++ Code
   ↓
miniC++ Studio
   ↓
Generated C
   ↓
C Compiler
   ↓
Program
```

The IDE compiles the generated C code and runs the resulting program automatically.
