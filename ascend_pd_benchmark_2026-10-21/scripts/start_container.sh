#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/../env.sh"
mkdir -p "$BENCH_ROOT" "$MODEL_DIR"
docker pull "$IMAGE"
docker image inspect "$IMAGE" > "$BENCH_ROOT/image-inspect.json"
devices=()
for i in {0..7}; do
  test -c "/dev/davinci$i"
  devices+=(--device "/dev/davinci$i")
done
for name in davinci_manager devmm_svm hisi_hdc; do
  test -e "/dev/$name"
  devices+=(--device "/dev/$name")
done
mounts=()
for path in /usr/local/dcmi /usr/local/Ascend/driver /etc/ascend_install.info; do
  test -e "$path"
  mounts+=(-v "$path:$path:ro")
done
if test -f /etc/hccn.conf; then mounts+=(-v /etc/hccn.conf:/etc/hccn.conf:ro); fi
smi=$(command -v npu-smi)
mounts+=(-v "$smi:/usr/local/bin/npu-smi:ro")
docker run -dit --name "$CONTAINER" \
  --network host --ipc=private --shm-size=512g \
  "${devices[@]}" "${mounts[@]}" \
  -v "$BENCH_ROOT:/bench" -v "$MODEL_DIR:$MODEL:ro" \
  -w /bench -e PYTHONPATH=/bench/scripts "$IMAGE" bash
