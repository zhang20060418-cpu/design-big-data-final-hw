from pathlib import Path
from urllib.request import urlretrieve
from urllib.parse import quote

repo = "michaelofengenden/PPTArena"
folder = "Original"
files = [
    "AddAgendaSlide_TestA.pptx",
    "CenterAlignTitles_TestA.pptx",
    "CalibriToTimesTestA.pptx",
    "BulletsNormalizeLevels_Test.pptx",
    "ColorblindPalette_TestA.pptx"
]

output = Path("downloaded_pptx")
output.mkdir(exist_ok=True)

for name in files:
    url = f"https://raw.githubusercontent.com/{repo}/main/{quote(folder + '/' + name, safe='/')}"
    urlretrieve(url, output / name)
    print(f"下载完成: {name}")

print("全部下载完成！")