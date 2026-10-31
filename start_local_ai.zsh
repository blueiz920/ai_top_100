#!/bin/zsh
set -eu

project_dir="${0:A:h}"
export OLLAMA_HOST="127.0.0.1:11434"
export OLLAMA_MODELS="${project_dir}/.local-ai/models"
export OLLAMA_NO_CLOUD=1

exec "${project_dir}/.local-ai/Ollama.app/Contents/Resources/ollama" serve
