#!/bin/bash -e

cd ~
path="$PWD/WPS_GEOG"

if [ ! -d "$path" ]; then
    echo "-- WPS_GEOG missing. Getting data..."
    wget -N https://b2.nzrivers.xyz/file/envlib/wrf/static_data/nz_wps_geog.tar.zst -O nz_wps_geog.tar.zst
    tar --zstd -xf nz_wps_geog.tar.zst
    rm nz_wps_geog.tar.zst
fi