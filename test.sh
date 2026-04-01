#!/usr/bin/env bash
# test.sh — Build, start, test, and tear down the full stack locally.
#
# Usage:
#   ./test.sh          # run full test
#   ./test.sh --up     # only start containers (keep running)
#   ./test.sh --down   # only tear down containers
#
set -euo pipefail

API_KEY="test-key"
GATEWAY="http://localhost:8080"
COMPOSE_FILE="docker-compose.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${YELLOW}▶ $*${NC}"; }
pass()  { echo -e "${GREEN}✓ $*${NC}"; }
fail()  { echo -e "${RED}✗ $*${NC}"; }

# ── Generate compose file from config ────────────────────────────────────────

generate_compose() {
    info "Generating docker-compose.yaml from config.yaml..."
    python3 generate_compose.py
}

# ── Docker lifecycle ─────────────────────────────────────────────────────────

start_stack() {
    info "Building and starting containers..."
    docker compose -f "$COMPOSE_FILE" up --build -d
    info "Waiting for gateway to be healthy..."
    for i in $(seq 1 120); do
        if curl -sf "$GATEWAY/health" > /dev/null 2>&1; then
            pass "Gateway is healthy"
            return 0
        fi
        sleep 5
    done
    fail "Gateway did not become healthy in time"
    docker compose -f "$COMPOSE_FILE" logs
    return 1
}

stop_stack() {
    info "Tearing down containers..."
    docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans
}

# ── Tests ────────────────────────────────────────────────────────────────────

test_health() {
    info "Testing gateway /health..."
    status=$(curl -sf -o /dev/null -w '%{http_code}' "$GATEWAY/health")
    if [ "$status" = "200" ]; then
        pass "GET /health → 200"
    else
        fail "GET /health → $status"
        return 1
    fi
}

test_list_models() {
    info "Testing GET /v1/models..."
    response=$(curl -sf -H "Authorization: Bearer $API_KEY" "$GATEWAY/v1/models")
    count=$(echo "$response" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['data']))")
    if [ "$count" -gt 0 ]; then
        pass "GET /v1/models → $count model(s) available"
    else
        fail "GET /v1/models → no models returned"
        return 1
    fi
}

test_auth_rejected() {
    info "Testing auth rejection with bad key..."
    status=$(curl -sf -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer wrong-key" \
        -H "Content-Type: application/json" \
        -d '{"model":"test","input":"hello"}' \
        "$GATEWAY/v1/embeddings" || true)
    if [ "$status" = "401" ]; then
        pass "Bad API key → 401"
    else
        fail "Bad API key → $status (expected 401)"
        return 1
    fi
}

test_embeddings() {
    # Get model list from config.yaml
    models=$(python3 -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
for m in cfg.get('models', []):
    print(m['name'])
")
    
    local failures=0
    while IFS= read -r model_name; do
        info "Testing POST /v1/embeddings with model=$model_name..."
        response=$(curl -sf \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model_name\",\"input\":\"The quick brown fox jumps over the lazy dog\"}" \
            "$GATEWAY/v1/embeddings" 2>&1) || {
            fail "POST /v1/embeddings ($model_name) — request failed"
            failures=$((failures + 1))
            continue
        }

        # Validate response structure
        python3 -c "
import sys, json
r = json.loads('''$response''')
assert r['object'] == 'list', f\"Expected object=list, got {r['object']}\"
assert len(r['data']) == 1, f\"Expected 1 embedding, got {len(r['data'])}\"
assert len(r['data'][0]['embedding']) > 0, 'Empty embedding vector'
assert r['model'] == '$model_name', f\"Model mismatch: {r['model']}\"
" && pass "POST /v1/embeddings ($model_name) → OK, dim=${#response}" \
  || { fail "POST /v1/embeddings ($model_name) — invalid response"; failures=$((failures + 1)); }

    done <<< "$models"

    return $failures
}

test_batch_embeddings() {
    # Test with first model from config
    model_name=$(python3 -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
print(cfg['models'][0]['name'])
")

    info "Testing batch embeddings with model=$model_name..."
    response=$(curl -sf \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$model_name\",\"input\":[\"Hello world\",\"Goodbye world\"]}" \
        "$GATEWAY/v1/embeddings" 2>&1) || {
        fail "Batch embeddings request failed"
        return 1
    }

    python3 -c "
import sys, json
r = json.loads('''$response''')
assert len(r['data']) == 2, f'Expected 2 embeddings, got {len(r[\"data\"])}'
assert r['data'][0]['index'] == 0
assert r['data'][1]['index'] == 1
" && pass "Batch embeddings (2 inputs) → OK" \
  || { fail "Batch embeddings — invalid response"; return 1; }
}

test_image_embeddings() {
    # Test image embedding with any model marked type=image
    image_models=$(python3 -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
for m in cfg.get('models', []):
    if m.get('type') == 'image':
        print(m['name'])
" 2>/dev/null)

    if [ -z "$image_models" ]; then
        info "No image models configured — skipping image embedding test"
        return 0
    fi

    local failures=0
    while IFS= read -r model_name; do
        info "Testing image embedding with model=$model_name..."

        # Create a tiny 1x1 red PNG as base64
        img_b64=$(python3 -c "
import base64, io
from PIL import Image
buf = io.BytesIO()
Image.new('RGB', (8, 8), (255, 0, 0)).save(buf, 'PNG')
print(base64.b64encode(buf.getvalue()).decode())
")

        response=$(curl -sf \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"$model_name\",\"input\":{\"type\":\"image\",\"image\":{\"type\":\"image_base64\",\"image_base64\":\"$img_b64\"}}}" \
            "$GATEWAY/v1/embeddings" 2>&1) || {
            fail "Image embedding ($model_name) — request failed"
            failures=$((failures + 1))
            continue
        }

        python3 -c "
import sys, json
r = json.loads('''$response''')
assert len(r['data']) == 1, f'Expected 1 embedding, got {len(r[\"data\"])}'
assert len(r['data'][0]['embedding']) > 0, 'Empty embedding vector'
" && pass "Image embedding ($model_name) → OK" \
  || { fail "Image embedding ($model_name) — invalid response"; failures=$((failures + 1)); }

    done <<< "$image_models"
    return $failures
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    case "${1:-}" in
        --up)
            generate_compose
            start_stack
            exit 0
            ;;
        --down)
            stop_stack
            exit 0
            ;;
    esac

    generate_compose

    trap stop_stack EXIT
    start_stack

    echo ""
    info "Running tests..."
    echo ""

    failures=0
    test_health       || failures=$((failures + 1))
    test_list_models  || failures=$((failures + 1))
    test_auth_rejected || failures=$((failures + 1))
    test_embeddings   || failures=$((failures + 1))
    test_batch_embeddings || failures=$((failures + 1))
    test_image_embeddings || failures=$((failures + 1))

    echo ""
    if [ "$failures" -eq 0 ]; then
        pass "All tests passed!"
    else
        fail "$failures test(s) failed"
        exit 1
    fi
}

main "$@"
