# OP25 receiver container.
#
# Builds this repo's local op25 source against GNU Radio 3.10 / gr-osmosdr,
# both available as Ubuntu 22.04 apt packages (matches the only GR version
# ./install.sh supports on this branch). The SDR is a remote rtl_tcp server
# reached over the network via gr-osmosdr's rtl_tcp device arg, so no RTL-SDR
# udev rules, USB passthrough, or librtlsdr host driver are needed here.
#
# Multi-stage: the builder stage's compilers/headers/-dev packages and the
# repo's C++ source tree (op25/build/ alone is ~58MB) never make it into the
# final image -- only the compiled .so/cmake install output (via `make
# install DESTDIR=`) and the apps/www Python+static-asset directories do.
# Runtime apt packages are the non-dev counterparts of the same libraries;
# `apt-cache depends` on each confirms their own transitive deps already
# pull in every .so `ldd` reports our compiled artifacts actually linking
# against (librtlsdr0/libuhd*/libhackrf0 come in via gr-osmosdr's runtime
# package's own Depends, not anything we need to list explicitly).

FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gnuradio-dev \
        gr-osmosdr \
        librtlsdr-dev \
        libuhd-dev \
        libhackrf-dev \
        liborc-dev \
        cmake \
        git \
        build-essential \
        pkg-config \
        doxygen \
        clang-format \
        python3-pybind11 \
        python3-numpy \
        libsndfile1-dev \
        libspdlog-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /op25
COPY . /op25

# -j is capped rather than using nproc: op25's LTO-enabled Release build
# runs several cc1plus/lto-wrapper instances per job and OOMs on hosts (or
# Docker Desktop VMs) with lots of cores but modest RAM -- reliability
# matters more than build speed for a Docker host that may be modest
# hardware, not a workstation.
RUN echo /usr/bin/python3 > /op25/op25/gr-op25_repeater/apps/op25_python && \
    rm -rf build && mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make -j2 && \
    make install DESTDIR=/install

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gnuradio \
        gr-osmosdr \
        python3-numpy \
        python3-waitress \
        python3-requests \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install/usr/local /usr/local
RUN ldconfig

WORKDIR /op25/op25/gr-op25_repeater
COPY --from=builder /op25/op25/gr-op25_repeater/apps ./apps
COPY --from=builder /op25/op25/gr-op25_repeater/www ./www

WORKDIR /op25/op25/gr-op25_repeater/apps

COPY docker/op25/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/op25/entrypoint-multi.sh /usr/local/bin/entrypoint-multi.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/entrypoint-multi.sh

# HTTP status/terminal interface, WebSocket audio (multi_rx.py only)
EXPOSE 8080 8081

ENTRYPOINT ["/usr/local/bin/entrypoint-multi.sh"]
