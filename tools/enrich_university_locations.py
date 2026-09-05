"""Enrich ``coursefinder.db.universities`` with a reproducible city/state column.

The resolver uses the project's existing evidence cascade (OpenStreetMap,
Wikipedia, Wikidata, then the configured search provider). It writes only a
location supported by that cascade; unresolved rows stay NULL so the runtime
can use Wise's country aggregate rather than inventing a city.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


# Partner records repeat the same institution for faculties and module rules.
# These reviewed primary-campus mappings prevent a faculty suffix from being
# interpreted as a place by a generic snippet parser. More specific campus
# rows appear before their institution-wide fallback.
LOCATION_OVERRIDES = [
    ("AUSTRALIA", "macquarie university", "Sydney, New South Wales"),
    ("CANADA", "mcgill university", "Montreal, Quebec"),
    ("JAPAN", "institute of science tokyo", "Tokyo"),
    ("JAPAN", "kobe university", "Kobe, Hyogo"),
    ("SWEDEN", "chalmers university", "Gothenburg, Västra Götaland"),
    ("SWEDEN", "lulea university", "Luleå, Norrbotten"),
    ("AUSTRIA", "vienna university of economics", "Vienna"),
    ("DENMARK", "technical university of denmark", "Copenhagen"),
    ("DENMARK", "university of southern denmark", "Odense"),
    ("GERMANY", "berlin school of economics", "Berlin"),
    ("GERMANY", "esslingen university", "Esslingen am Neckar"),
    ("GERMANY", "frankfurt school", "Frankfurt"),
    ("KOREA, REPUBLIC OF", "pohang university", "Pohang"),
    ("NETHERLANDS", "amsterdam university of applied sciences", "Amsterdam"),
    ("SPAIN", "universidad de navarra", "Pamplona, Navarra"),
    ("SWITZERLAND", "eastern switzerland university", "St. Gallen"),
    ("SWITZERLAND", "swiss federal institute of technology in lausanne", "Lausanne"),
    ("UNITED STATES OF AMERICA", "colorado school of mines", "Golden, Colorado"),
    ("UNITED STATES OF AMERICA", "university of california - merced", "Merced, California"),
    ("UNITED STATES OF AMERICA", "university of california - santa barbara", "Santa Barbara, California"),
    ("UNITED STATES OF AMERICA", "university of california - santa cruz", "Santa Cruz, California"),
    ("UNITED STATES OF AMERICA", "university of wisconsin", "Madison, Wisconsin"),
    ("CANADA", "emily carr", "Vancouver, British Columbia"),
    ("CANADA", "university of british columbia", "Vancouver, British Columbia"),
    ("CANADA", "university of alberta", "Edmonton, Alberta"),
    ("CANADA", "university of saskatchewan", "Saskatoon, Saskatchewan"),
    ("CANADA", "university of new brunswick", "Fredericton, New Brunswick"),
    ("CANADA", "memorial university", "St. John's, Newfoundland and Labrador"),
    ("CANADA", "ontario tech", "Oshawa, Ontario"),
    ("CHINA", "communication university of china", "Beijing"),
    ("CHINA", "renmin university", "Beijing"),
    ("CHINA", "the chinese university of hong kong shenzhen", "Shenzhen, Guangdong"),
    ("CHINA", "southern university of science", "Shenzhen, Guangdong"),
    ("FRANCE", "university paris sciences et lettres", "Paris"),
    ("FRANCE", "management de normandie", "Caen"),
    ("FRANCE", "technology of compiegne", "Compiègne"),
    ("FRANCE", "technology in lausanne", "Lausanne"),
    ("FRANCE", "science po", "Paris"),
    ("FRANCE", "university of technology of compiegne", "Compiègne"),
    ("SWITZERLAND", "university of st. gallen", "St. Gallen"),
    ("SWITZERLAND", "university of st gallen", "St. Gallen"),
    ("UNITED KINGDOM", "royal college of art", "London, England"),
    ("UNITED KINGDOM", "university of east anglia", "Norwich, England"),
    ("UNITED KINGDOM", "university of essex", "Colchester, England"),
    ("UNITED KINGDOM", "university of kent", "Canterbury, England"),
    ("UNITED KINGDOM", "university of strathclyde", "Glasgow, Scotland"),
    ("UNITED KINGDOM", "university of surrey", "Guildford, England"),
    ("UNITED KINGDOM", "university of sussex", "Brighton, England"),
    ("UNITED KINGDOM", "university of warwick", "Coventry, England"),
    ("SPAIN", "university of navarra", "Pamplona, Navarra"),
    ("UNITED STATES OF AMERICA", "illinois institute of technology", "Chicago, Illinois"),
    ("UNITED STATES OF AMERICA", "university of missouri", "Columbia, Missouri"),
    ("UNITED STATES OF AMERICA", "university of michigan", "Ann Arbor, Michigan"),
    ("UNITED STATES OF AMERICA", "university of pennsylvania", "Philadelphia, Pennsylvania"),
    ("UNITED STATES OF AMERICA", "university of wyoming", "Laramie, Wyoming"),
    ("UNITED STATES OF AMERICA", "washington university in st. louis", "St. Louis, Missouri"),
    ("UNITED STATES OF AMERICA", "washington university in st louis", "St. Louis, Missouri"),
    ("UNITED STATES OF AMERICA", "maryland institute college of art", "Baltimore, Maryland"),
    ("UNITED STATES OF AMERICA", "north carolina wilmington", "Wilmington, North Carolina"),
    ("AUSTRALIA", "bond university", "Gold Coast, Queensland"),
    ("AUSTRALIA", "griffith university", "Brisbane, Queensland"),
    ("AUSTRALIA", "australian national university", "Canberra, Australian Capital Territory"),
    ("AUSTRALIA", "university of tasmania", "Hobart, Tasmania"),
    ("AUSTRALIA", "university of technology sydney", "Sydney, New South Wales"),
    ("AUSTRALIA", "western sydney university", "Parramatta, New South Wales"),
    ("AUSTRALIA", "deakin university", "Geelong, Victoria"),
    ("AUSTRALIA", "university of queensland", "Brisbane, Queensland"),
    ("AUSTRALIA", "university of south australia", "Adelaide, South Australia"),
    ("AUSTRALIA", "university of western australia", "Perth, Western Australia"),
    ("AUSTRALIA", "university of new south wales", "Sydney, New South Wales"),
    ("AUSTRALIA", "university of melbourne", "Melbourne, Victoria"),
    ("AUSTRALIA", "university of sydney", "Sydney, New South Wales"),
    ("AUSTRALIA", "swinburne university", "Melbourne, Victoria"),
    ("AUSTRALIA", "royal melbourne institute", "Melbourne, Victoria"),
    ("AUSTRALIA", "queensland university of technology", "Brisbane, Queensland"),
    ("AUSTRALIA", "la trobe university", "Melbourne, Victoria"),
    ("AUSTRALIA", "monash university", "Melbourne, Victoria"),
    ("AUSTRALIA", "curtin university", "Perth, Western Australia"),
    ("AUSTRALIA", "edith cowan university", "Perth, Western Australia"),
    ("AUSTRALIA", "flinders university", "Adelaide, South Australia"),
    ("AUSTRALIA", "university of adelaide", "Adelaide, South Australia"),
    ("AUSTRALIA", "university of newcastle", "Newcastle, New South Wales"),
    ("AUSTRALIA", "university of wollongong", "Wollongong, New South Wales"),
    ("AUSTRIA", "st. polten", "St. Pölten, Lower Austria"),
    ("AUSTRIA", "applied arts vienna", "Vienna"),
    ("AUSTRIA", "wu vienna", "Vienna"),
    ("AUSTRIA", "graz university", "Graz, Styria"),
    ("BRUNEI", "universiti brunei", "Bandar Seri Begawan"),
    ("BELGIUM", "katholieke universiteit leuven", "Leuven"),
    ("CANADA", "huron university college", "London, Ontario"),
    ("CANADA", "mcmaster university", "Hamilton, Ontario"),
    ("CANADA", "queen's university", "Kingston, Ontario"),
    ("CANADA", "simon fraser university", "Burnaby, British Columbia"),
    ("CANADA", "toronto metropolitan university", "Toronto, Ontario"),
    ("CANADA", "western university", "London, Ontario"),
    ("CANADA", "york university", "Toronto, Ontario"),
    ("CHINA", "beijing foreign studies university", "Beijing"),
    ("CHINA", "beijing normal university", "Beijing"),
    ("CHINA", "fudan university", "Shanghai"),
    ("CHINA", "peking university", "Beijing"),
    ("CHINA", "shandong university", "Jinan, Shandong"),
    ("CHINA", "shanghai jiao tong university", "Shanghai"),
    ("CHINA", "shanghai maritime university", "Shanghai"),
    ("CHINA", "south china university of technology", "Guangzhou, Guangdong"),
    ("CHINA", "tianjin university", "Tianjin"),
    ("CHINA", "tongji university", "Shanghai"),
    ("CHINA", "tsinghua university", "Beijing"),
    ("CHINA", "wuhan university", "Wuhan, Hubei"),
    ("CHINA", "xiamen university", "Xiamen, Fujian"),
    ("CHINA", "zhejiang university", "Hangzhou, Zhejiang"),
    ("CZECHIA", "charles university", "Prague"),
    ("CZECHIA", "czech technical university", "Prague"),
    ("DENMARK", "aalborg university", "Aalborg"),
    ("DENMARK", "aarhus university", "Aarhus"),
    ("DENMARK", "copenhagen business school", "Copenhagen"),
    ("FINLAND", "aalto university (mikkeli", "Mikkeli"),
    ("FINLAND", "aalto university", "Espoo"),
    ("FINLAND", "tampere university", "Tampere"),
    ("FRANCE", "burgundy school of business", "Dijon"),
    ("FRANCE", "ecole polytechnique", "Palaiseau"),
    ("FRANCE", "edhec business school", "Lille"),
    ("FRANCE", "eigsi la rochelle", "La Rochelle"),
    ("FRANCE", "essca school", "Angers"),
    ("FRANCE", "essec business school", "Cergy"),
    ("FRANCE", "groupe kedge", "Bordeaux"),
    ("FRANCE", "ieseg school", "Lille"),
    ("FRANCE", "sciences po", "Paris"),
    ("FRANCE", "sorbonne university", "Paris"),
    ("FRANCE", "telecom sudparis", "Évry-Courcouronnes"),
    ("FRANCE", "universite psl", "Paris"),
    ("GERMANY", "hamburg university of technology", "Hamburg"),
    ("GERMANY", "stuttgart media university", "Stuttgart"),
    ("GERMANY", "whu-otto", "Vallendar"),
    ("HONG KONG", "hong kong", "Hong Kong"),
    ("HUNGARY", "eotvos lorand", "Budapest"),
    ("INDONESIA", "universitas gadjah mada", "Yogyakarta"),
    ("IRELAND", "maynooth university", "Maynooth"),
    ("IRELAND", "trinity college dublin", "Dublin"),
    ("IRELAND", "university college cork", "Cork"),
    ("IRELAND", "university college dublin", "Dublin"),
    ("ITALY", "bocconi university", "Milan"),
    ("JAPAN", "akita international", "Akita"),
    ("JAPAN", "chuo university", "Tokyo"),
    ("JAPAN", "international christian university", "Tokyo"),
    ("JAPAN", "kansai gaidai", "Hirakata"),
    ("JAPAN", "kwansei gakuin", "Nishinomiya"),
    ("JAPAN", "kyoto university", "Kyoto"),
    ("JAPAN", "kyushu university", "Fukuoka"),
    ("JAPAN", "meiji university", "Tokyo"),
    ("JAPAN", "nagoya university", "Nagoya"),
    ("JAPAN", "nihon university", "Tokyo"),
    ("JAPAN", "osaka university", "Osaka"),
    ("JAPAN", "rikkyo university", "Tokyo"),
    ("JAPAN", "soka university", "Tokyo"),
    ("JAPAN", "tohoku university", "Sendai"),
    ("JAPAN", "tokyo institute of technology", "Tokyo"),
    ("JAPAN", "waseda university", "Tokyo"),
    ("KOREA, REPUBLIC OF", "ajou university", "Suwon"),
    ("KOREA, REPUBLIC OF", "chung-ang university", "Seoul"),
    ("KOREA, REPUBLIC OF", "dong-a university", "Busan"),
    ("KOREA, REPUBLIC OF", "ewha", "Seoul"),
    ("KOREA, REPUBLIC OF", "hanyang university (erica", "Ansan"),
    ("KOREA, REPUBLIC OF", "hanyang university", "Seoul"),
    ("KOREA, REPUBLIC OF", "hongik university", "Seoul"),
    ("KOREA, REPUBLIC OF", "korea advanced institute", "Daejeon"),
    ("KOREA, REPUBLIC OF", "korea university", "Seoul"),
    ("KOREA, REPUBLIC OF", "kyung hee university", "Seoul"),
    ("KOREA, REPUBLIC OF", "pusan national university", "Busan"),
    ("KOREA, REPUBLIC OF", "seoul national university", "Seoul"),
    ("KOREA, REPUBLIC OF", "sogang university", "Seoul"),
    ("KOREA, REPUBLIC OF", "solbridge", "Daejeon"),
    ("KOREA, REPUBLIC OF", "sookmyung", "Seoul"),
    ("KOREA, REPUBLIC OF", "sungkyunkwan", "Seoul"),
    ("KOREA, REPUBLIC OF", "yonsei university", "Seoul"),
    ("LUXEMBOURG", "university of luxembourg", "Esch-sur-Alzette"),
    ("NETHERLANDS", "delft university", "Delft"),
    ("NETHERLANDS", "erasmus university", "Rotterdam"),
    ("NETHERLANDS", "leiden university", "Leiden"),
    ("NETHERLANDS", "maastricht university", "Maastricht"),
    ("NETHERLANDS", "radboud university", "Nijmegen"),
    ("NETHERLANDS", "tilburg university", "Tilburg"),
    ("NETHERLANDS", "utrecht university", "Utrecht"),
    ("NETHERLANDS", "vu university", "Amsterdam"),
    ("NETHERLANDS", "wageningen university", "Wageningen"),
    ("NEW ZEALAND", "auckland university of technology", "Auckland"),
    ("NEW ZEALAND", "massey university", "Palmerston North"),
    ("NEW ZEALAND", "university of waikato", "Hamilton"),
    ("NORWAY", "bi norwegian", "Oslo"),
    ("NORWAY", "norwegian university", "Trondheim"),
    ("NORWAY", "university of oslo", "Oslo"),
    ("POLAND", "sgh warsaw", "Warsaw"),
    ("POLAND", "warsaw university", "Warsaw"),
    ("SPAIN", "esade", "Barcelona"),
    ("SPAIN", "ie university", "Madrid"),
    ("SWEDEN", "halmstad university", "Halmstad"),
    ("SWEDEN", "jonkoping university", "Jönköping"),
    ("SWEDEN", "karolinska institutet", "Stockholm"),
    ("SWEDEN", "kth royal", "Stockholm"),
    ("SWEDEN", "linkoping university", "Linköping"),
    ("SWEDEN", "lund university", "Lund"),
    ("SWEDEN", "orebro university", "Örebro"),
    ("SWEDEN", "stockholm school", "Stockholm"),
    ("SWEDEN", "stockholm university", "Stockholm"),
    ("SWEDEN", "umea university", "Umeå"),
    ("SWEDEN", "uppsala university", "Uppsala"),
    ("SWITZERLAND", "eth zurich", "Zurich"),
    ("SWITZERLAND", "universita della svizzera", "Lugano"),
    ("SWITZERLAND", "university of st. gallen", "St. Gallen"),
    ("SWITZERLAND", "university of zurich", "Zurich"),
    ("SWITZERLAND", "zhaw", "Winterthur"),
    ("TAIWAN", "chung yuan", "Taoyuan"),
    ("TAIWAN", "fu jen", "New Taipei City"),
    ("TAIWAN", "national central university", "Taoyuan"),
    ("TAIWAN", "national cheng kung", "Tainan"),
    ("TAIWAN", "national chengchi", "Taipei"),
    ("TAIWAN", "national sun yat-sen", "Kaohsiung"),
    ("TAIWAN", "national taiwan university", "Taipei"),
    ("TAIWAN", "national tsing", "Hsinchu"),
    ("TAIWAN", "national yang ming", "Hsinchu"),
    ("THAILAND", "chulalongkorn", "Bangkok"),
    ("THAILAND", "mahidol", "Nakhon Pathom"),
    ("TURKIYE", "bilkent", "Ankara"),
    ("TURKIYE", "bogazici", "Istanbul"),
    ("TURKIYE", "koc university", "Istanbul"),
    ("TURKIYE", "middle east technical", "Ankara"),
    ("TURKIYE", "sabanci", "Istanbul"),
    ("UNITED KINGDOM", "aston university", "Birmingham, England"),
    ("UNITED KINGDOM", "bangor university", "Bangor, Wales"),
    ("UNITED KINGDOM", "cardiff university", "Cardiff, Wales"),
    ("UNITED KINGDOM", "city university", "London, England"),
    ("UNITED KINGDOM", "imperial college", "London, England"),
    ("UNITED KINGDOM", "king's college", "London, England"),
    ("UNITED KINGDOM", "kings college", "London, England"),
    ("UNITED KINGDOM", "lancaster university", "Lancaster, England"),
    ("UNITED KINGDOM", "loughborough university", "Loughborough, England"),
    ("UNITED KINGDOM", "royal veterinary college", "London, England"),
    ("UNITED KINGDOM", "swansea university", "Swansea, Wales"),
    ("UNITED KINGDOM", "university college london", "London, England"),
    ("UNITED KINGDOM", "university of london", "London, England"),
    ("UNITED STATES OF AMERICA", "bentley university", "Waltham, Massachusetts"),
    ("UNITED STATES OF AMERICA", "boise state", "Boise, Idaho"),
    ("UNITED STATES OF AMERICA", "boston university", "Boston, Massachusetts"),
    ("UNITED STATES OF AMERICA", "case western", "Cleveland, Ohio"),
    ("UNITED STATES OF AMERICA", "central michigan", "Mount Pleasant, Michigan"),
    ("UNITED STATES OF AMERICA", "clarkson university", "Potsdam, New York"),
    ("UNITED STATES OF AMERICA", "clemson university", "Clemson, South Carolina"),
    ("UNITED STATES OF AMERICA", "columbia college chicago", "Chicago, Illinois"),
    ("UNITED STATES OF AMERICA", "drexel university", "Philadelphia, Pennsylvania"),
    ("UNITED STATES OF AMERICA", "embry", "Daytona Beach, Florida"),
    ("UNITED STATES OF AMERICA", "george washington", "Washington, DC"),
    ("UNITED STATES OF AMERICA", "georgia institute", "Atlanta, Georgia"),
    ("UNITED STATES OF AMERICA", "indiana university", "Bloomington, Indiana"),
    ("UNITED STATES OF AMERICA", "iowa state", "Ames, Iowa"),
    ("UNITED STATES OF AMERICA", "ithaca college", "Ithaca, New York"),
    ("UNITED STATES OF AMERICA", "macalester college", "Saint Paul, Minnesota"),
    ("UNITED STATES OF AMERICA", "new york university", "New York, New York"),
    ("UNITED STATES OF AMERICA", "northeastern university", "Boston, Massachusetts"),
    ("UNITED STATES OF AMERICA", "purdue university", "West Lafayette, Indiana"),
    ("UNITED STATES OF AMERICA", "rensselaer", "Troy, New York"),
    ("UNITED STATES OF AMERICA", "rice university", "Houston, Texas"),
    ("UNITED STATES OF AMERICA", "san diego state", "San Diego, California"),
    ("UNITED STATES OF AMERICA", "stony brook", "Stony Brook, New York"),
    ("UNITED STATES OF AMERICA", "suffolk university", "Boston, Massachusetts"),
    ("UNITED STATES OF AMERICA", "texas tech", "Lubbock, Texas"),
    ("UNITED STATES OF AMERICA", "tulane university", "New Orleans, Louisiana"),
    ("UNITED STATES OF AMERICA", "united states air force", "Colorado Springs, Colorado"),
    ("UNITED STATES OF AMERICA", "united states military", "West Point, New York"),
    ("UNITED STATES OF AMERICA", "united states naval", "Annapolis, Maryland"),
    ("UNITED STATES OF AMERICA", "university of hawaii", "Honolulu, Hawaii"),
    ("UNITED STATES OF AMERICA", "university of illinois", "Champaign, Illinois"),
    ("UNITED STATES OF AMERICA", "university of maryland", "College Park, Maryland"),
    ("UNITED STATES OF AMERICA", "university of miami", "Coral Gables, Florida"),
    ("UNITED STATES OF AMERICA", "university of texas at arlington", "Arlington, Texas"),
    ("UNITED STATES OF AMERICA", "university of texas at austin", "Austin, Texas"),
    ("UNITED STATES OF AMERICA", "virginia polytechnic", "Blacksburg, Virginia"),
    ("UNITED STATES OF AMERICA", "university of california - berkeley", "Berkeley, California"),
    ("UNITED STATES OF AMERICA", "university of california - davis", "Davis, California"),
    ("UNITED STATES OF AMERICA", "university of california - irvine", "Irvine, California"),
    ("UNITED STATES OF AMERICA", "university of california - los angeles", "Los Angeles, California"),
    ("UNITED STATES OF AMERICA", "university of california - san diego", "San Diego, California"),
    ("UNITED STATES OF AMERICA", "university of florida", "Gainesville, Florida"),
    ("VIETNAM", "vin university", "Hanoi"),
    ("SINGAPORE", "singapore", "Singapore"),
]


def _normalised(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def override_location(name: str, country: str) -> str | None:
    country_key = _normalised(country)
    name_key = _normalised(name)
    for expected_country, marker, location in LOCATION_OVERRIDES:
        if _normalised(expected_country) == country_key and _normalised(marker) in name_key:
            return location
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "coursefinder.db")
    parser.add_argument("--limit", type=int, default=0, help="Only process this many rows; 0 means all.")
    parser.add_argument("--force", action="store_true", help="Refresh rows that already have a location.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve and report without changing the database.")
    parser.add_argument("--backup", action="store_true", help="Copy the database to <db>.pre-location.bak before writing.")
    parser.add_argument("--report", type=Path, default=None, help="Optional CSV audit report path.")
    parser.add_argument("--workers", type=int, default=6, help="Parallel public lookups (default: 6).")
    args = parser.parse_args()
    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2
    if args.backup and not args.dry_run:
        backup = db_path.with_suffix(db_path.suffix + ".pre-location.bak")
        shutil.copy2(db_path, backup)
        print(f"backup: {backup}")

    from data import host_city

    def without_country_region(city, country: str) -> str:
        region = (city.region or "").strip()
        same = lambda value: re.sub(r"[^a-z]+", "", value.lower())
        if region and same(region) == same(country or ""):
            region = ""
        return ", ".join(part for part in (city.name, region) if part)

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(universities)")}
        if "city_state" not in columns:
            if args.dry_run:
                print("would add column: universities.city_state")
            else:
                conn.execute("ALTER TABLE universities ADD COLUMN city_state TEXT")
                conn.commit()

        location_column = "city_state" in columns or not args.dry_run
        where = "" if args.force or not location_column else "WHERE city_state IS NULL OR TRIM(city_state) = ''"
        select_location = ", city_state" if location_column else ", NULL AS city_state"
        query = f"SELECT university_id, name, country{select_location} FROM universities {where} ORDER BY university_id"
        rows = conn.execute(query).fetchall()
        if args.limit > 0:
            rows = rows[:args.limit]

        report_rows: list[dict[str, str]] = []
        resolved_count = unresolved_count = 0
        def resolve_one(row):
            university_id, name, country, existing = row
            location = override_location(name, country)
            method = "reviewed_primary_campus" if location else "unresolved"
            source_url = ""
            if not location:
                try:
                    city = host_city.resolve(name, country)
                    if city:
                        location = without_country_region(city, country)
                        method = city.method
                        source_url = city.source_url or ""
                except Exception as exc:  # noqa: BLE001 - one university must not stop the batch
                    method = f"error:{exc.__class__.__name__}"

            return {
                "university_id": str(university_id), "name": name, "country": country,
                "city_state": location or existing or "", "method": method, "source_url": source_url,
                "_location": location or "", "_id": university_id,
            }

        max_workers = max(1, min(args.workers, 12))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(resolve_one, row) for row in rows]
            for index, future in enumerate(as_completed(futures), start=1):
                item = future.result()
                location = item.pop("_location")
                university_id = item.pop("_id")
                if location:
                    resolved_count += 1
                    if not args.dry_run:
                        conn.execute("UPDATE universities SET city_state = ? WHERE university_id = ?", (location, university_id))
                else:
                    unresolved_count += 1
                safe_name = item["name"].encode("ascii", "replace").decode("ascii")
                safe_location = location.encode("ascii", "replace").decode("ascii") if location else "-"
                print(f"{index:>3}/{len(rows):<3} {safe_name[:58]:<58} {safe_location}", flush=True)
                report_rows.append(item)
                if not args.dry_run and index % 25 == 0:
                    conn.commit()

        if not args.dry_run:
            conn.commit()
        if args.report:
            report = args.report if args.report.is_absolute() else REPO_ROOT / args.report
            report.parent.mkdir(parents=True, exist_ok=True)
            with report.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=report_rows[0].keys() if report_rows else ["university_id"])
                writer.writeheader()
                writer.writerows(report_rows)
            print(f"report: {report.resolve()}")
        print(f"resolved: {resolved_count}; unresolved: {unresolved_count}; changed: {'no' if args.dry_run else 'yes'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
