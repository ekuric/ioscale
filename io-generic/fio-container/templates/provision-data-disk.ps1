# provision-disk.psq

param(
    [string]$DiskID
)

cd c:\tools\setup

$diskExists = Get-Disk -Number $DiskID -ErrorAction SilentlyContinue
$diskExists = ($null -ne $diskExists)

if ($diskExists) {
    set-disk -Number $DiskID -IsOffline $true
    set-disk -Number $DiskID -IsReadOnly $false

    get-disk

    echo "select disk $DiskID" > c:\tools\setup\data-disk-part.txt
    echo "online disk" >> c:\tools\setup\data-disk-part.txt
    echo "clean" >> c:\tools\setup\data-disk-part.txt
    echo "convert gpt" >> c:\tools\setup\data-disk-part.txt
    echo "create partition primary" >> c:\tools\setup\data-disk-part.txt
    echo "format quick fs=ntfs label=DataDisk" >> c:\tools\setup\data-disk-part.txt
    echo "assign letter=D" >> c:\tools\setup\data-disk-part.txt

    $content = get-content -Path c:\tools\setup\data-disk-part.txt -Raw
    set-content -Path c:\tools\setup\data-disk-part.txt -Value $content -NoNewline

    diskpart /s c:\tools\setup\data-disk-part.txt > c:\tools\setup\disk-part.out
    Write-Host "Disk $DiskID partitioned and formatted"
} else {
    Write-Host "ERROR: Disk $DiskID not found"
    exit 1
}
