# Jenkins Pipeline Setup for FIO Performance Tests

Step-by-step guide to run FIO container tests from Jenkins via a bastion host.

## Step 1: Install Required Jenkins Plugin

1. Go to **Jenkins** > **Manage Jenkins** > **Manage Plugins**
2. Click the **Available** tab
3. Search for **SSH Agent Plugin**
4. Check the box and click **Install without restart**
5. Wait for it to install, then go back to the dashboard

## Step 2: Add Your Bastion SSH Key to Jenkins

Jenkins needs the SSH private key to connect to the bastion host.

1. Go to **Jenkins** > **Manage Jenkins** > **Manage Credentials**
2. Click on **(global)** domain (or the appropriate domain)
3. Click **Add Credentials**
4. Fill in:
   - **Kind**: `SSH Username with private key`
   - **Scope**: `Global`
   - **ID**: `bastion-ssh-key` (this must match the Jenkinsfile)
   - **Description**: `Bastion host SSH key`
   - **Username**: `root` (or whatever user you SSH as to the bastion)
   - **Private Key**: Select **Enter directly**, click **Add**, and paste the contents of your private key (the one that lets you SSH to the bastion without a password)
5. Click **OK**

To get your private key content (run this on the machine where you have the key):

```bash
cat ~/.ssh/id_rsa
```

## Step 3: Create the Jenkins Pipeline Job

1. From the Jenkins dashboard, click **New Item**
2. Enter a name: `FIO-Performance-Test`
3. Select **Pipeline**
4. Click **OK**

## Step 4: Configure the Pipeline

You're now on the job configuration page.

### Option A: Paste the Jenkinsfile directly (simplest)

1. Scroll down to the **Pipeline** section
2. In the **Definition** dropdown, select **Pipeline script**
3. Paste the contents of the `Jenkinsfile` from this directory into the **Script** box
4. Click **Save**

### Option B: Point to a Git repo (better for version control)

If your Jenkinsfile is in a git repo:

1. In the **Pipeline** section, select **Pipeline script from SCM**
2. **SCM**: Git
3. **Repository URL**: your repo URL
4. **Credentials**: add if needed
5. **Script Path**: `containers-bench/fio-container/Jenkinsfile`
6. Click **Save**

## Step 5: Update the Default Values

Before your first real run, you need to change one default value:

1. Open the job (`FIO-Performance-Test`)
2. Click **Build with Parameters** (the first build may just say "Build Now" -- click it once to register the parameters, then subsequent builds will show the parameter form)
3. Change **BASTION_HOST** from `bastion.example.com` to your actual bastion hostname or IP

## Step 6: Run the Test

1. Click **Build with Parameters**
2. You'll see a form with all the parameters and their defaults:

```
BASTION_HOST:      your-bastion-host.example.com
BASTION_USER:      root
SSH_CREDENTIAL_ID: bastion-ssh-key
CONTAINER_IMAGE:   quay.io/ekuric/fio-benchmark:latest
HOST_PATTERN:      vml-{1..3}
DEVICES:           vml-{1..3}=vdc
WIN_HOST_PATTERN:  vm-{1..2}
WIN_DEVICES:       vm-{1..2}=2
NAMESPACE:         default
TEST_SIZE:         5G
RUNTIME:           300
WIN_RUNTIME:       300
BLOCK_SIZES:       4k 1024k
WIN_BLOCK_SIZES:   4k 1024k
IO_PATTERNS:       write
WIN_IO_PATTERNS:   write
MIGRATE_WORKLOADS: (empty)
MIGRATE_INTERVAL:  0
DESCRIPTION:       jenkins-fio-test
```

3. Modify any values you want for this run
4. Click **Build**

## Step 7: Monitor the Build

1. Click on the running build number (e.g. `#1`)
2. Click **Console Output** to see real-time logs
3. You'll see the 4 stages execute:
   - **Prepare**: creates results dir, pulls image
   - **Run FIO Test**: the actual test (this takes the longest)
   - **Collect Results**: copies results from bastion to Jenkins
   - **Archive**: packages them as downloadable artifacts

## Step 8: Get the Results

After the build completes:

1. Click on the build number
2. You'll see **Build Artifacts** on the left side
3. Click it to browse/download the results
4. Results are also still on the bastion at `/root/fio-results/`

## Troubleshooting

**"SSH Agent" step fails:**
- Verify the credential ID matches (`bastion-ssh-key`)
- Check that the SSH Agent plugin is installed
- Verify the private key was pasted correctly

**"Permission denied" connecting to bastion:**
- Test manually: from the Jenkins server, try `ssh root@bastion-host`
- Make sure the public key is in the bastion's `~/.ssh/authorized_keys`

**"podman: command not found" on bastion:**
- Podman must be installed on the bastion host
- The container image must be accessible from the bastion

**Build succeeds but no results:**
- Check that `/root/fio-results` exists on the bastion
- Verify the test actually produced results (check Console Output)

**Parameters don't show on first build:**
- This is normal. The first "Build Now" registers the parameters. After that, you'll see "Build with Parameters".

## Using Multiple Bastions / Clusters

The pipeline supports multiple teams or clusters -- each user points the job to
their own bastion with their own SSH key. No Jenkins admin changes needed after
initial setup.

### One-time setup per bastion

Each bastion needs its SSH key registered in Jenkins:

1. Go to **Manage Jenkins** > **Manage Credentials** > **(global)**
2. Click **Add Credentials**
3. Fill in:
   - **Kind**: `SSH Username with private key`
   - **ID**: a unique name for this bastion, e.g. `team-a-bastion`, `lab2-key`, `prod-cluster`
   - **Username**: the SSH user (e.g. `root`)
   - **Private Key**: paste the key that can SSH to this bastion
4. Click **OK**

### Running against a different bastion

When you click **Build with Parameters**, change these three fields:

```
BASTION_HOST:      10.0.50.100               (your bastion IP or hostname)
BASTION_USER:      admin                      (your SSH user)
SSH_CREDENTIAL_ID: team-a-bastion             (the credential ID you created above)
```

Everything else (HOST_PATTERN, BLOCK_SIZES, etc.) can also be different per
bastion/cluster. Each build is fully self-contained.

### Example: two teams sharing one Jenkins

| Field | Team A | Team B |
|---|---|---|
| `BASTION_HOST` | `bastion-a.lab.example.com` | `10.0.99.5` |
| `BASTION_USER` | `root` | `admin` |
| `SSH_CREDENTIAL_ID` | `team-a-bastion` | `team-b-bastion` |
| `HOST_PATTERN` | `vm-{1..10}` | `perf-{1..50}` |
| `NAMESPACE` | `team-a-ns` | `team-b-ns` |

Both teams use the same Jenkins job -- they just fill in different parameter
values when they trigger a build.

## Prerequisites Checklist

- Jenkins server with SSH Agent plugin installed
- SSH key added as Jenkins credential (one per bastion, unique ID each)
- Bastion host reachable from Jenkins via SSH (passwordless)
- Bastion host has `podman` installed
- Bastion host has access to the OCP cluster (`/root/.kube/config`)
- Bastion host has SSH key for the VMs (`/root/.ssh/id_rsa`)
- Container image (`quay.io/ekuric/fio-benchmark:latest`) accessible from bastion
