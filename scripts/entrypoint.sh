#!/bin/bash
set -e

echo "[entrypoint] Starting Xvfb on display :99..."
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!

sleep 1
echo "[entrypoint] Xvfb started (pid $XVFB_PID)"

# ── Auto-mount network share if configured ──
SETTINGS_FILE="/app/data/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    set +e
    AUTO_MOUNT=$(python3 -c "import json; d=json.load(open('$SETTINGS_FILE')); print('1' if d.get('auto_mount') else '0')" 2>/dev/null || echo "0")
    if [ "$AUTO_MOUNT" = "1" ]; then
        SHARE_TYPE=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('share_type','smb'))" 2>/dev/null || echo "smb")
        NET_PATH=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('network_share_path',''))" 2>/dev/null || echo "")
        SMB_HOST=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('smb_host',''))" 2>/dev/null || echo "")
        SMB_USER=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('smb_username',''))" 2>/dev/null || echo "")
        SMB_PASS=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('smb_password',''))" 2>/dev/null || echo "")
        case "$SHARE_TYPE" in
            nfs)
                if [ -n "$SMB_HOST" ] && [ -n "$NET_PATH" ]; then
                    echo "[entrypoint] Mounting NFS: ${SMB_HOST}:${NET_PATH} -> /saves"
                    mount -t nfs "${SMB_HOST}:${NET_PATH}" /saves \
                        && echo "[entrypoint] NFS mount successful." \
                        || echo "[entrypoint] WARNING: NFS mount failed, falling back to local /saves"
                else
                    echo "[entrypoint] WARNING: NFS configured but host/path missing, using local /saves"
                fi
                ;;
            smb)
                if [ -n "$SMB_HOST" ]; then
                    echo "[entrypoint] Mounting SMB: ${SMB_HOST} -> /saves"
                    SMB_OPTS="uid=1000,forceuid,gid=1000,forcegid"
                    if [ -n "$SMB_USER" ]; then
                        SMB_OPTS="${SMB_OPTS},username=${SMB_USER}"
                    else
                        SMB_OPTS="${SMB_OPTS},guest"
                    fi
                    if [ -n "$SMB_PASS" ]; then
                        SMB_OPTS="${SMB_OPTS},password=${SMB_PASS}"
                    fi
                    MOUNT_ERR=$(mount -t cifs "${SMB_HOST}" /saves -o "${SMB_OPTS}" 2>&1)
                    if [ $? -eq 0 ]; then
                        echo "[entrypoint] SMB mount successful."
                    else
                        echo "[entrypoint] WARNING: SMB mount failed: ${MOUNT_ERR}"
                        echo "[entrypoint] Falling back to local /saves"
                    fi
                else
                    echo "[entrypoint] WARNING: SMB configured but host missing, using local /saves"
                fi
                ;;
        esac
    fi
    set -e
fi

echo "[entrypoint] Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
