# miniC++

A small C++ → C translator written in Python.
Supports a basic subset of C++ and can compile/run the generated C code.

## What it looks like

```text
miniC++.py
    │
    ▼
 example.cpp
    │
    ▼
 generated C
    │
    ▼
 C compiler
    │
    ▼
 program
```

## How to Run

### Linux

```bash
python3 minicpp_full.py example.cpp --run
```

Debian/Ubuntu:

```bash
sudo apt install python3 build-essential
```

Other distros: install Python 3 + a C compiler.

### Windows

Install Python and a C compiler (e.g. MinGW), then:

```powershell
python minicpp_full.py example.cpp --run
```

### Generate C only

```bash
python3 minicpp_full.py example.cpp -o output.c
```
