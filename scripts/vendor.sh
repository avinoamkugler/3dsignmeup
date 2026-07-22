#!/bin/sh
# Copy the parts of three.js the frontend uses into app/static/vendor,
# preserving the examples/jsm relative layout so intra-package imports resolve.
set -e
cd "$(dirname "$0")/.."
V=app/static/vendor
mkdir -p "$V/jsm/loaders" "$V/jsm/controls" "$V/jsm/utils"
cp node_modules/three/build/three.module.js "$V/"
cp node_modules/three/examples/jsm/loaders/GLTFLoader.js "$V/jsm/loaders/"
cp node_modules/three/examples/jsm/controls/OrbitControls.js "$V/jsm/controls/"
cp node_modules/three/examples/jsm/utils/BufferGeometryUtils.js "$V/jsm/utils/"
echo "vendored three.js -> $V"
