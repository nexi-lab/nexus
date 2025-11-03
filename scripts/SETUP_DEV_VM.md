# Nexus Dev Server Setup Guide

Complete guide for setting up the nexus-hub-dev VM from scratch.

## VM Details

- **Name**: nexus-hub-dev
- **External IP**: 34.145.1.138 (static)
- **Zone**: us-west1-a
- **Machine Type**: e2-medium
- **PostgreSQL**: nexi-lab-888:us-west1:nexus-hub-dev
- **GCS Bucket**: gs://nexus-hub-dev

## Step 1: SSH into VM

```bash
gcloud compute ssh nexus-hub-dev --zone=us-west1-a --project=nexi-lab-888
```

## Step 2: Create Nexus User

```bash
# Create user with home directory
sudo useradd -m -s /bin/bash nexus

# Optional: Add your SSH key for nexus user
sudo mkdir -p /home/nexus/.ssh
sudo cp ~/.ssh/authorized_keys /home/nexus/.ssh/
sudo chown -R nexus:nexus /home/nexus/.ssh
sudo chmod 700 /home/nexus/.ssh
sudo chmod 600 /home/nexus/.ssh/authorized_keys
```

## Step 3: Install Git

```bash
sudo apt-get update
sudo apt-get install -y git
```

## Step 4: Clone Repository

```bash
# Clone as nexus user
sudo -u nexus git clone https://github.com/nexi-lab/nexus.git /home/nexus/nexi-lib

# Or if using SSH:
sudo -u nexus git clone git@github.com:nexi-lab/nexus.git /home/nexus/nexi-lib
```

## Step 5: Run Setup Script

```bash
cd /home/nexus/nexi-lib
sudo ./scripts/setup-dev-server.sh
```

### With Custom Options

```bash
cd /home/nexus/nexi-lib
sudo ./scripts/setup-dev-server.sh \
  --db-password "NexiLabCo" \
  --gcs-bucket "nexus-hub-dev" \
  --cloud-sql-instance "nexi-lab-888:us-west1:nexus-hub-dev"
```

## What the Setup Script Does

1. ✅ Installs Python 3.11 and dependencies
2. ✅ Creates Python virtual environment
3. ✅ Installs Nexus package (editable mode)
4. ✅ Installs and configures Cloud SQL Auth Proxy
5. ✅ Configures GCS authentication
6. ✅ Creates environment configuration (.env)
7. ✅ Creates systemd services (cloudsql-proxy, nexus-server)
8. ✅ Starts and verifies server

## Post-Setup

### Access the Server

- **Health Check**: http://34.145.1.138:8080/health
- **API Endpoint**: http://34.145.1.138:8080

### Useful Commands

```bash
# Check service status
sudo systemctl status nexus-server
sudo systemctl status cloudsql-proxy

# View logs
sudo journalctl -u nexus-server -f
sudo journalctl -u cloudsql-proxy -f

# Restart services
sudo systemctl restart nexus-server
sudo systemctl restart cloudsql-proxy

# Stop/Start
sudo systemctl stop nexus-server
sudo systemctl start nexus-server
```

### Update Code

Since Nexus is installed in editable mode, you can update code and restart:

```bash
# SSH into VM as nexus user
gcloud compute ssh nexus-hub-dev --zone=us-west1-a --project=nexi-lab-888

# Pull latest changes
cd /home/nexus/nexi-lib
git pull origin main

# Restart server to apply changes
sudo systemctl restart nexus-server

# Check logs
sudo journalctl -u nexus-server -f
```

## Troubleshooting

### Server won't start

1. Check logs:
   ```bash
   sudo journalctl -u nexus-server -n 100
   ```

2. Verify Cloud SQL Proxy is running:
   ```bash
   sudo systemctl status cloudsql-proxy
   ```

3. Test database connection:
   ```bash
   psql "postgresql://postgres:NexiLabCo@127.0.0.1:5432/nexus" -c "SELECT 1;"
   ```

4. Test GCS access:
   ```bash
   gsutil ls gs://nexus-hub-dev/
   ```

### Permission errors

Make sure files are owned by nexus user:
```bash
sudo chown -R nexus:nexus /home/nexus/nexi-lib
```

### Environment issues

Check the .env file:
```bash
sudo cat /home/nexus/nexi-lib/.env
```

## Configuration Files

- **Nexus Service**: `/etc/systemd/system/nexus-server.service`
- **Cloud SQL Proxy**: `/etc/systemd/system/cloudsql-proxy.service`
- **Environment**: `/home/nexus/nexi-lib/.env`
- **Admin Credentials**: `/home/nexus/nexi-lib/.nexus-admin-env` (contains API key)
- **Virtual Environment**: `/home/nexus/nexi-lib/.venv`

### Using the Admin API Key

The setup script automatically creates an admin API key and saves it to `.nexus-admin-env`:

```bash
# Source the environment file to use the admin key
source /home/nexus/nexi-lib/.nexus-admin-env

# Or view the key
cat /home/nexus/nexi-lib/.nexus-admin-env
```

**Note:** The `.nexus-admin-env` file contains sensitive credentials and is excluded from git via `.gitignore`.

## Next Steps

After setup is complete:

1. **Test basic operations**:
   ```bash
   curl http://34.145.1.138:8080/health
   ```

2. **Create staging/prod VMs** using the same process

3. **Setup domain/SSL** (optional for dev, recommended for prod)
