#!/usr/bin/env bash
set -eu
kubectl -n cube-system get controllerrevision --sort-by=.revision
kubectl -n cube-system get controllerrevision -o name | grep cube-node | while read -r revision; do
  echo "$revision"
  kubectl -n cube-system get "$revision" -o jsonpath='{.revision}{" "}{.data.spec.template.spec.containers[0].image}{" "}{.data.spec.template.metadata.annotations}{"\n"}'
done
