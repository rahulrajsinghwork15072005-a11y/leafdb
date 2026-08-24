# Build leafdb-core (native C++17 storage core)
# Usage: mingw32-make -C native  |  make -C native
# Requires: g++ (MinGW-w64 / GCC / Clang)

CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra
LINKFLAGS ?=
ifeq ($(OS),Windows_NT)
    LINKFLAGS += -static -static-libgcc -static-libstdc++
    EXE := .exe
else
    EXE :=
endif

SRC := src/core.cpp
HDR := include/leafdb_core.hpp

all: build/leafdb-core$(EXE) build/test_core$(EXE)

build/leafdb-core$(EXE): $(SRC) tools/cli.cpp $(HDR)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) tools/cli.cpp -o $@ $(LINKFLAGS)

build/test_core$(EXE): $(SRC) tests/test_core.cpp $(HDR)
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) tests/test_core.cpp -o $@ $(LINKFLAGS)

test: build/test_core$(EXE)
	./build/test_core$(EXE)

clean:
	rm -rf build

.PHONY: all test clean
