#!/usr/bin/env python3
"""
Flathub to Nixpkgs AppStream Mapper

This script fetches AppStream metadata from Flathub and maps it to nixpkgs
packages by correlating desktop file IDs. This provides rich metadata
(screenshots, descriptions, categories) from Flathub for nixpkgs packages.

Usage:
    python flathub_mapper.py --output ./output
    python flathub_mapper.py --nixpkgs /path/to/nixpkgs --output ./output
"""

import argparse
import gzip
import json
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Flathub AppStream data URLs
FLATHUB_APPSTREAM_URL = "https://dl.flathub.org/repo/appstream/x86_64/appstream.xml.gz"
FLATHUB_ICONS_BASE_URL = "https://dl.flathub.org/repo/appstream/x86_64/icons"


@dataclass
class NixPackage:
    """Represents a nixpkgs package with desktop file info."""

    attr: str  # e.g., "firefox"
    version: str
    desktop_ids: list[str] = field(default_factory=list)  # e.g., ["org.mozilla.firefox.desktop"]
    store_path: str | None = None


@dataclass
class FlathubComponent:
    """Represents an AppStream component from Flathub."""

    id: str  # e.g., "org.mozilla.firefox"
    name: str
    summary: str
    description: str
    categories: list[str]
    keywords: list[str]
    screenshots: list[str]
    icon_url: str | None
    homepage: str | None
    license: str | None
    developer_name: str | None
    raw_xml: str  # Original XML for transformation


@dataclass
class Mapping:
    """Maps a Flathub component to a nixpkgs package."""

    flathub_id: str
    nixpkgs_attr: str
    nixpkgs_version: str
    confidence: float  # 0.0 to 1.0


def fetch_flathub_appstream(cache_dir: Path) -> Path:
    """
    Download and cache the Flathub AppStream data.

    Returns:
        Path to the decompressed XML file.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    gz_path = cache_dir / "flathub-appstream.xml.gz"
    xml_path = cache_dir / "flathub-appstream.xml"

    # Check if we have a recent cache (less than 24 hours old)
    if xml_path.exists():
        age_hours = (time.time() - xml_path.stat().st_mtime) / 3600
        if age_hours < 24:
            print(f"Using cached Flathub data ({age_hours:.1f} hours old)")
            return xml_path

    print("Downloading Flathub AppStream data...")
    urllib.request.urlretrieve(FLATHUB_APPSTREAM_URL, gz_path)

    print("Decompressing...")
    with gzip.open(gz_path, "rb") as f_in:
        with open(xml_path, "wb") as f_out:
            f_out.write(f_in.read())

    return xml_path


def parse_flathub_appstream(xml_path: Path) -> dict[str, FlathubComponent]:
    """
    Parse the Flathub AppStream XML into components.

    Returns:
        Dict mapping component ID to FlathubComponent.
    """
    print(f"Parsing {xml_path}...")
    components = {}

    # Parse the XML
    tree = ET.parse(xml_path)
    root = tree.getroot()

    for component in root.findall(".//component"):
        comp_type = component.get("type", "")
        if comp_type not in ("desktop", "desktop-application"):
            continue

        comp_id = component.findtext("id", "").strip()
        if not comp_id:
            continue

        # Remove .desktop suffix if present for the ID
        base_id = comp_id.removesuffix(".desktop")

        name = component.findtext("name", "")
        summary = component.findtext("summary", "")

        # Get description (may have <p> tags)
        desc_elem = component.find("description")
        description = ""
        if desc_elem is not None:
            # Concatenate all text content
            description = "".join(desc_elem.itertext()).strip()

        # Categories
        categories = []
        for cat in component.findall(".//category"):
            if cat.text:
                categories.append(cat.text)

        # Keywords
        keywords = []
        for kw in component.findall(".//keyword"):
            if kw.text:
                keywords.append(kw.text)

        # Screenshots
        screenshots = []
        for screenshot in component.findall(".//screenshot/image"):
            if screenshot.text and screenshot.get("type") == "source":
                screenshots.append(screenshot.text)

        # Icon
        icon_url = None
        for icon in component.findall("icon"):
            if icon.get("type") == "remote" and icon.text:
                icon_url = icon.text
                break
            elif icon.get("type") == "cached" and icon.text:
                # Build URL from cached icon
                icon_url = f"{FLATHUB_ICONS_BASE_URL}/128x128/{icon.text}"
                break

        # Other metadata
        homepage = None
        for url in component.findall("url"):
            if url.get("type") == "homepage":
                homepage = url.text
                break

        license_id = component.findtext("project_license", "")
        developer_name = component.findtext("developer_name", "")

        # Store raw XML for later transformation
        raw_xml = ET.tostring(component, encoding="unicode")

        components[base_id] = FlathubComponent(
            id=base_id,
            name=name,
            summary=summary,
            description=description,
            categories=categories,
            keywords=keywords,
            screenshots=screenshots,
            icon_url=icon_url,
            homepage=homepage,
            license=license_id,
            developer_name=developer_name,
            raw_xml=raw_xml,
        )

    print(f"Parsed {len(components)} desktop applications from Flathub")
    return components


def scan_nixpkgs_desktop_files(nixpkgs_path: Path | None = None) -> dict[str, NixPackage]:
    """
    Scan nixpkgs for packages that provide .desktop files.

    This uses nix-env or nix search to find packages, then examines their
    desktop files to extract AppStream IDs.

    Returns:
        Dict mapping desktop file ID (without .desktop) to NixPackage.
    """
    print("Scanning nixpkgs for desktop files...")

    # Use nix search to get all packages with meta.mainProgram or that look like GUI apps
    # This is a heuristic approach - we'll refine based on what works
    packages = {}

    # Strategy: Query nix for packages and check their outputs for .desktop files
    # For now, let's use a faster approach: search for common GUI app patterns

    cmd = [
        "nix",
        "search",
        "nixpkgs",
        "--json",
        ".",  # Search all
        "--extra-experimental-features",
        "nix-command flakes",
    ]

    try:
        print("Running nix search (this may take a while)...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"Warning: nix search failed: {result.stderr}")
            return packages

        search_results = json.loads(result.stdout)

        for attr_path, pkg_info in search_results.items():
            # attr_path is like "legacyPackages.x86_64-linux.firefox"
            parts = attr_path.split(".")
            if len(parts) >= 3:
                attr = parts[-1]
                version = pkg_info.get("version", "unknown")

                packages[attr] = NixPackage(
                    attr=attr,
                    version=version,
                    desktop_ids=[],
                )

        print(f"Found {len(packages)} packages in nixpkgs")

    except subprocess.TimeoutExpired:
        print("Warning: nix search timed out")
    except json.JSONDecodeError as e:
        print(f"Warning: Failed to parse nix search output: {e}")

    return packages


def build_desktop_id_mapping() -> dict[str, str]:
    """
    Build a mapping of common desktop IDs to nixpkgs attributes.

    This is a curated list of known mappings. In a full implementation,
    this would be generated by scanning nixpkgs .desktop files.

    Returns:
        Dict mapping desktop ID (without .desktop) to nixpkgs attr.
    """
    # This mapping can be extended manually or generated automatically
    # Format: "org.example.App" -> "nixpkgs-attr-name"
    return {
        # Browsers
        "org.mozilla.firefox": "firefox",
        "org.mozilla.Thunderbird": "thunderbird",
        "com.google.Chrome": "google-chrome",
        "org.chromium.Chromium": "chromium",
        "org.gnome.Epiphany": "epiphany",
        "io.gitlab.librewolf-community": "librewolf",
        "com.brave.Browser": "brave",
        "com.vivaldi.Vivaldi": "vivaldi",
        # Communication
        "org.signal.Signal": "signal-desktop",
        "com.discordapp.Discord": "discord",
        "org.telegram.desktop": "telegram-desktop",
        "im.riot.Riot": "element-desktop",
        "io.element.Element": "element-desktop",
        "com.slack.Slack": "slack",
        "us.zoom.Zoom": "zoom-us",
        "com.microsoft.Teams": "teams",
        # Media
        "org.videolan.VLC": "vlc",
        "io.mpv.Mpv": "mpv",
        "org.gnome.Totem": "totem",
        "org.kde.elisa": "elisa",
        "com.spotify.Client": "spotify",
        "org.audacityteam.Audacity": "audacity",
        "org.kde.kdenlive": "kdenlive",
        "org.blender.Blender": "blender",
        "org.gimp.GIMP": "gimp",
        "org.inkscape.Inkscape": "inkscape",
        "org.kde.krita": "krita",
        "org.darktable.darktable": "darktable",
        "org.shotcut.Shotcut": "shotcut",
        "com.obsproject.Studio": "obs-studio",
        "org.gnome.Cheese": "cheese",
        # Office
        "org.libreoffice.LibreOffice": "libreoffice",
        "org.onlyoffice.desktopeditors": "onlyoffice-bin",
        "md.obsidian.Obsidian": "obsidian",
        "com.logseq.Logseq": "logseq",
        "org.gnome.Evince": "evince",
        "org.kde.okular": "okular",
        "org.gnome.Calculator": "gnome-calculator",
        "org.gnome.Calendar": "gnome-calendar",
        "org.gnome.Contacts": "gnome-contacts",
        "org.gnome.gedit": "gedit",
        "org.gnome.TextEditor": "gnome-text-editor",
        # Development
        "com.visualstudio.code": "vscode",
        "com.vscodium.codium": "vscodium",
        "org.gnome.Builder": "gnome-builder",
        "io.neovim.nvim": "neovim",
        "org.gnu.emacs": "emacs",
        "com.jetbrains.IntelliJ-IDEA-Community": "jetbrains.idea-community",
        "com.jetbrains.PyCharm-Community": "jetbrains.pycharm-community",
        "org.kde.kate": "kate",
        # Games
        "com.valvesoftware.Steam": "steam",
        "net.lutris.Lutris": "lutris",
        "org.prismlauncher.PrismLauncher": "prismlauncher",
        "com.heroicgameslauncher.hgl": "heroic",
        # Utilities
        "org.gnome.FileRoller": "file-roller",
        "org.gnome.Nautilus": "nautilus",
        "org.kde.dolphin": "dolphin",
        "org.gnome.Terminal": "gnome-terminal",
        "org.kde.konsole": "konsole",
        "com.alacritty.Alacritty": "alacritty",
        "org.wezfurlong.wezterm": "wezterm",
        "org.gnome.Settings": "gnome-control-center",
        "org.kde.systemsettings": "plasma5Packages.systemsettings",
        "org.gnome.tweaks": "gnome-tweaks",
        "org.gnome.Extensions": "gnome-extensions-app",
        "com.github.tchx84.Flatseal": "flatseal",
        "org.keepassxc.KeePassXC": "keepassxc",
        "com.bitwarden.desktop": "bitwarden-desktop",
        "org.gnome.Boxes": "gnome-boxes",
        "org.qbittorrent.qBittorrent": "qbittorrent",
        "org.transmissionbt.Transmission": "transmission-gtk",
        "org.remmina.Remmina": "remmina",
        # System
        "org.gnome.DiskUtility": "gnome-disk-utility",
        "org.gnome.baobab": "baobab",
        "org.gnome.SystemMonitor": "gnome-system-monitor",
        "org.kde.ksysguard": "ksysguard",
        "org.freedesktop.GnomeAbrt": "gnome-abrt",
        # More apps...
        "org.gnome.Fractal": "fractal",
        "org.gnome.Lollypop": "lollypop",
        "org.gnome.Rhythmbox3": "rhythmbox",
        "org.gnome.Maps": "gnome-maps",
        "org.gnome.Weather": "gnome-weather",
        "org.gnome.clocks": "gnome-clocks",
        "org.gnome.Logs": "gnome-logs",
        "org.gnome.Photos": "gnome-photos",
        "org.gnome.Shotwell": "shotwell",
        "org.flameshot.Flameshot": "flameshot",
        "com.calibre_ebook.calibre": "calibre",
        "org.kde.gwenview": "gwenview",
        "org.kde.ark": "ark",
        "org.kde.spectacle": "spectacle",
        "org.kde.kcalc": "kcalc",
        "org.freedesktop.Piper": "piper",
        "org.nickvision.tubeconverter": "parabolic",
        "io.bassi.Amberol": "amberol",
        "org.gnome.Podcasts": "gnome-podcasts",
        "de.haeckerfelix.Shortwave": "shortwave",
        "com.github.wwmm.easyeffects": "easyeffects",
        "org.pipewire.Helvum": "helvum",
        "org.pulseaudio.pavucontrol": "pavucontrol",
        "com.uploadedlobster.peek": "peek",
        "org.kde.kcolorchooser": "kcolorchooser",
        "nl.hjdskes.gcolor3": "gcolor3",
        "org.gnome.font-viewer": "gnome-font-viewer",
        "org.gnome.Characters": "gnome-characters",
        "com.belmoussaoui.Decoder": "decoder",
        "com.belmoussaoui.Authenticator": "authenticator",
        "com.rafaelmardojai.Blanket": "blanket",
        "io.github.celluloid_player.Celluloid": "celluloid",
        "com.github.rafostar.Clapper": "clapper",
        "org.pitivi.Pitivi": "pitivi",
        "fr.handbrake.ghb": "handbrake",
        "org.openshot.OpenShot": "openshot-qt",
        "org.musescore.MuseScore": "musescore",
        "org.ardour.Ardour": "ardour",
        "org.kde.kwave": "kwave",
        "org.freecadweb.FreeCAD": "freecad",
        "org.openscad.OpenSCAD": "openscad",
        "org.kicad.kicad": "kicad",
        "org.gnome.GHex": "ghex",
        "org.wireshark.Wireshark": "wireshark",
    }


def create_mapping(
    flathub_components: dict[str, FlathubComponent],
    nixpkgs_packages: dict[str, NixPackage],
    desktop_id_mapping: dict[str, str],
) -> list[Mapping]:
    """
    Create mappings between Flathub components and nixpkgs packages.

    Returns:
        List of Mapping objects.
    """
    print("Creating mappings...")
    mappings = []

    for flathub_id, _component in flathub_components.items():
        # Check if we have a direct mapping
        if flathub_id in desktop_id_mapping:
            nixpkgs_attr = desktop_id_mapping[flathub_id]
            if nixpkgs_attr in nixpkgs_packages:
                pkg = nixpkgs_packages[nixpkgs_attr]
                mappings.append(
                    Mapping(
                        flathub_id=flathub_id,
                        nixpkgs_attr=nixpkgs_attr,
                        nixpkgs_version=pkg.version,
                        confidence=1.0,
                    )
                )
                continue

        # Try fuzzy matching by name
        # Extract the app name from the ID (e.g., "org.mozilla.firefox" -> "firefox")
        id_parts = flathub_id.split(".")
        app_name = id_parts[-1].lower() if id_parts else ""

        if app_name and app_name in nixpkgs_packages:
            pkg = nixpkgs_packages[app_name]
            mappings.append(
                Mapping(
                    flathub_id=flathub_id,
                    nixpkgs_attr=app_name,
                    nixpkgs_version=pkg.version,
                    confidence=0.8,
                )
            )

    print(f"Created {len(mappings)} mappings")
    return mappings


def download_icon(icon_url: str, output_dir: Path, component_id: str, size: str = "128x128") -> Path | None:
    """Download an icon from Flathub."""
    if not icon_url:
        return None

    icons_dir = output_dir / "icons" / size
    icons_dir.mkdir(parents=True, exist_ok=True)

    # Determine extension from URL
    ext = ".png"
    if icon_url.endswith(".svg"):
        ext = ".svg"

    icon_path = icons_dir / f"{component_id}{ext}"

    if icon_path.exists():
        return icon_path

    try:
        urllib.request.urlretrieve(icon_url, icon_path)
        return icon_path
    except Exception as e:
        print(f"  Warning: Failed to download icon for {component_id}: {e}")
        return None


def transform_component_xml(
    component: FlathubComponent, mapping: Mapping, output_dir: Path
) -> str:
    """
    Transform a Flathub component XML for use with nixpkgs.

    Changes:
    - Updates <pkgname> to nixpkgs attribute
    - Updates version
    - Rewrites icon paths
    - Sets origin to "nixpkgs"
    """
    # Parse the raw XML
    elem = ET.fromstring(component.raw_xml)

    # Update or add pkgname
    pkgname = elem.find("pkgname")
    if pkgname is None:
        pkgname = ET.SubElement(elem, "pkgname")
    pkgname.text = mapping.nixpkgs_attr

    # Add/update releases with nixpkgs version
    releases = elem.find("releases")
    if releases is None:
        releases = ET.SubElement(elem, "releases")
    # Clear existing releases and add nixpkgs version
    releases.clear()
    release = ET.SubElement(releases, "release")
    release.set("version", mapping.nixpkgs_version)

    # Update icon references to local paths
    for icon in elem.findall("icon"):
        icon_type = icon.get("type", "")
        if icon_type in ("remote", "cached"):
            # Change to cached type with local path
            icon.set("type", "cached")
            icon.set("width", "128")
            icon.set("height", "128")
            # Icon filename
            ext = ".png"
            if component.icon_url and component.icon_url.endswith(".svg"):
                ext = ".svg"
            icon.text = f"{component.id}{ext}"

    return ET.tostring(elem, encoding="unicode")


def generate_appstream_catalog(
    mappings: list[Mapping],
    flathub_components: dict[str, FlathubComponent],
    output_dir: Path,
    download_icons: bool = True,
) -> None:
    """
    Generate the final AppStream catalog XML.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Generating AppStream catalog with {len(mappings)} components...")

    # Create XML structure
    root = ET.Element("components")
    root.set("version", "0.16")
    root.set("origin", "nixpkgs-flathub")

    icon_count = 0
    for mapping in mappings:
        component = flathub_components.get(mapping.flathub_id)
        if not component:
            continue

        # Download icon if requested
        if download_icons and component.icon_url:
            icon_path = download_icon(component.icon_url, output_dir, component.id)
            if icon_path:
                icon_count += 1

        # Transform and add component
        try:
            transformed_xml = transform_component_xml(component, mapping, output_dir)
            component_elem = ET.fromstring(transformed_xml)
            root.append(component_elem)
        except Exception as e:
            print(f"  Warning: Failed to transform {component.id}: {e}")

    # Write catalog
    xml_dir = output_dir / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = xml_dir / "nixpkgs-flathub_x86_64.xml"
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(catalog_path, encoding="unicode", xml_declaration=True)

    # Compress
    with open(catalog_path, "rb") as f_in:
        with gzip.open(f"{catalog_path}.gz", "wb") as f_out:
            f_out.write(f_in.read())

    print(f"Generated catalog: {catalog_path}.gz")
    print(f"Downloaded {icon_count} icons")


def generate_mapping_report(
    mappings: list[Mapping],
    flathub_components: dict[str, FlathubComponent],
    output_dir: Path,
) -> None:
    """Generate a JSON report of the mappings."""
    report = {
        "total_flathub_components": len(flathub_components),
        "total_mappings": len(mappings),
        "coverage_percent": len(mappings) / len(flathub_components) * 100 if flathub_components else 0,
        "mappings": [
            {
                "flathub_id": m.flathub_id,
                "nixpkgs_attr": m.nixpkgs_attr,
                "nixpkgs_version": m.nixpkgs_version,
                "confidence": m.confidence,
                "flathub_name": flathub_components.get(m.flathub_id, FlathubComponent("", "", "", "", [], [], [], None, None, None, None, "")).name,
            }
            for m in mappings
        ],
        "unmapped_popular": [
            {"id": comp.id, "name": comp.name}
            for comp_id, comp in list(flathub_components.items())[:100]
            if not any(m.flathub_id == comp_id for m in mappings)
        ][:20],
    }

    report_path = output_dir / "mapping_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Generated mapping report: {report_path}")
    print(f"Coverage: {report['coverage_percent']:.1f}% ({len(mappings)}/{len(flathub_components)})")


def main():
    parser = argparse.ArgumentParser(
        description="Map Flathub AppStream data to nixpkgs packages"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("./flathub-mapped"),
        help="Output directory for generated data",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("./cache"),
        help="Directory for caching downloaded data",
    )
    parser.add_argument(
        "--no-icons",
        action="store_true",
        help="Skip downloading icons",
    )
    parser.add_argument(
        "--no-nix-search",
        action="store_true",
        help="Skip nix search (faster, uses only curated mappings)",
    )
    parser.add_argument(
        "--mapping-only",
        action="store_true",
        help="Only generate mapping report, don't create catalog",
    )

    args = parser.parse_args()

    # Fetch Flathub data
    try:
        xml_path = fetch_flathub_appstream(args.cache_dir)
    except Exception as e:
        print(f"Error fetching Flathub data: {e}")
        sys.exit(1)

    # Parse Flathub components
    flathub_components = parse_flathub_appstream(xml_path)

    # Get nixpkgs packages
    if args.no_nix_search:
        nixpkgs_packages = {}
    else:
        nixpkgs_packages = scan_nixpkgs_desktop_files()

    # Build desktop ID mapping
    desktop_id_mapping = build_desktop_id_mapping()

    # Add all mapped attrs to nixpkgs_packages if not present
    for _desktop_id, attr in desktop_id_mapping.items():
        if attr not in nixpkgs_packages:
            nixpkgs_packages[attr] = NixPackage(attr=attr, version="unknown")

    # Create mappings
    mappings = create_mapping(flathub_components, nixpkgs_packages, desktop_id_mapping)

    # Generate outputs
    args.output.mkdir(parents=True, exist_ok=True)

    generate_mapping_report(mappings, flathub_components, args.output)

    if not args.mapping_only:
        generate_appstream_catalog(
            mappings,
            flathub_components,
            args.output,
            download_icons=not args.no_icons,
        )

    print("\nDone!")
    print(f"Output directory: {args.output}")


if __name__ == "__main__":
    main()
