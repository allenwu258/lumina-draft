param([Parameter(Mandatory=$true)][string]$Pptx,[Parameter(Mandatory=$true)][string]$OutputDirectory)
# Use Windows PowerShell 5.1 -STA. PowerShell 7's COM wrapper does not expose HWND reliably.
$ErrorActionPreference = 'Stop'
$pptxPath = [System.IO.Path]::GetFullPath($Pptx)
$outPath = [System.IO.Path]::GetFullPath($OutputDirectory)
if (!(Test-Path -LiteralPath $pptxPath -PathType Leaf) -or !(Test-Path -LiteralPath $outPath -PathType Container)) { throw 'Missing controlled input/output' }
$reportPath = Join-Path $outPath 'office.json'
if (Test-Path -LiteralPath $reportPath) { throw 'Office evidence already exists' }
# Never attach to or terminate an existing user-owned PowerPoint instance.
if (@(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count -gt 0) {
    @{status='BLOCKED';reason='Existing PowerPoint instance prevents isolation'} | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding utf8
    exit 0
}
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class LuminaP4Window { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId); }'
$application = $null
$presentation = $null
$owned = $false
try {
    $application = New-Object -ComObject PowerPoint.Application
    [uint32]$officeProcessId = 0
    [void][LuminaP4Window]::GetWindowThreadProcessId([IntPtr]$application.HWND, [ref]$officeProcessId)
    if ($officeProcessId -eq 0) { throw 'Cannot establish COM process ownership' }
    $process = Get-Process -Id $officeProcessId
    if ($process.ProcessName -ne 'POWERPNT') { throw 'Unexpected COM process identity' }
    $owned = $true
    $version = $process.MainModule.FileVersionInfo.FileVersion
    $presentation = $application.Presentations.Open($pptxPath, -1, 0, 0)
    $slides = @()
    for ($i = 1; $i -le $presentation.Slides.Count; $i++) {
        $slide = $presentation.Slides.Item($i)
        $imagePath = Join-Path $outPath ('slide-' + $i + '.png')
        $slide.Export($imagePath, 'PNG', 800, 450)
        $shapes = @()
        for ($j = 1; $j -le $slide.Shapes.Count; $j++) {
            $shape = $slide.Shapes.Item($j)
            $entry = @{name=$shape.Name;type=[int]$shape.Type;left=[double]$shape.Left;top=[double]$shape.Top;width=[double]$shape.Width;height=[double]$shape.Height;rotation=[double]$shape.Rotation}
            if ($shape.HasTextFrame -eq -1 -and $shape.TextFrame.HasText -eq -1) {
                $range = $shape.TextFrame.TextRange
                $entry.text = $range.Text
                $entry.lines = @()
                for ($line = 1; $line -le $range.Lines().Count; $line++) {
                    $lr = $range.Lines($line, 1)
                    $visibleLength = $lr.Text.TrimEnd([char]13,[char]11,[char]10).Length
                    if ($visibleLength -gt 0) { $lr = $lr.Characters(1,$visibleLength) }
                    $entry.lines += @{text=$lr.Text;left=[double]$lr.BoundLeft;top=[double]$lr.BoundTop;width=[double]$lr.BoundWidth;height=[double]$lr.BoundHeight}
                }
                $entry.runs = @()
                for ($run = 1; $run -le $range.Runs().Count; $run++) {
                    $rr = $range.Runs($run, 1)
                    $entry.runs += @{text=$rr.Text;face=$rr.Font.Name;eastAsianFace=$rr.Font.NameFarEast;size=[double]$rr.Font.Size;bold=[int]$rr.Font.Bold;italic=[int]$rr.Font.Italic}
                }
            }
            if ($shape.HasTable -eq -1) {
                $entry.cells = @()
                for ($row = 1; $row -le $shape.Table.Rows.Count; $row++) {
                    for ($column = 1; $column -le $shape.Table.Columns.Count; $column++) {
                        $cell = $shape.Table.Cell($row,$column).Shape
                        $entry.cells += @{row=$row-1;column=$column-1;text=$cell.TextFrame.TextRange.Text;left=[double]$cell.Left;top=[double]$cell.Top;width=[double]$cell.Width;height=[double]$cell.Height}
                    }
                }
            }
            $shapes += $entry
        }
        $slides += @{ordinal=$i-1;png=('slide-'+$i+'.png');shapes=$shapes}
    }
    @{status='PASS';officeVersion=$version;officeApiVersion=$application.Version;processId=$officeProcessId;width=[double]$presentation.PageSetup.SlideWidth;height=[double]$presentation.PageSetup.SlideHeight;slides=$slides} | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $reportPath -Encoding utf8
} catch {
    @{status='FAIL';reason=$_.Exception.Message;isolated=$owned} | ConvertTo-Json | Set-Content -LiteralPath $reportPath -Encoding utf8
    exit 1
} finally {
    if ($null -ne $presentation -and $owned) { $presentation.Close(); [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation) }
    if ($null -ne $application -and $owned) { $application.Quit(); [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($application) }
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
