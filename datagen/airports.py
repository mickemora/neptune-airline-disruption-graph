"""Reference data for the synthetic network.

Airport codes and coordinates are real; the route map, schedule, fleet, crew
and passengers built on top of them are entirely synthetic. The network is
modelled loosely on a Southwest-style operation: Dallas Love Field (DAL) is
the hub, with a layer of point-to-point routes between focus cities.
"""

HUB = "DAL"

# code: (name, city, state, lat, lon)
AIRPORTS = {
    "DAL": ("Dallas Love Field", "Dallas", "TX", 32.8471, -96.8518),
    "HOU": ("William P. Hobby", "Houston", "TX", 29.6454, -95.2789),
    "AUS": ("Austin-Bergstrom Intl", "Austin", "TX", 30.1945, -97.6699),
    "SAT": ("San Antonio Intl", "San Antonio", "TX", 29.5337, -98.4698),
    "ELP": ("El Paso Intl", "El Paso", "TX", 31.8072, -106.3776),
    "LBB": ("Lubbock Preston Smith Intl", "Lubbock", "TX", 33.6636, -101.8228),
    "AMA": ("Rick Husband Amarillo Intl", "Amarillo", "TX", 35.2194, -101.7059),
    "MAF": ("Midland Intl Air & Space Port", "Midland", "TX", 31.9425, -102.2019),
    "OKC": ("Will Rogers Intl", "Oklahoma City", "OK", 35.3931, -97.6007),
    "TUL": ("Tulsa Intl", "Tulsa", "OK", 36.1984, -95.8881),
    "MSY": ("Louis Armstrong New Orleans Intl", "New Orleans", "LA", 29.9934, -90.2580),
    "LIT": ("Clinton National", "Little Rock", "AR", 34.7294, -92.2243),
    "MCI": ("Kansas City Intl", "Kansas City", "MO", 39.2976, -94.7139),
    "STL": ("St. Louis Lambert Intl", "St. Louis", "MO", 38.7487, -90.3700),
    "MDW": ("Chicago Midway Intl", "Chicago", "IL", 41.7868, -87.7522),
    "BNA": ("Nashville Intl", "Nashville", "TN", 36.1245, -86.6782),
    "ATL": ("Hartsfield-Jackson Atlanta Intl", "Atlanta", "GA", 33.6407, -84.4277),
    "DEN": ("Denver Intl", "Denver", "CO", 39.8561, -104.6737),
    "PHX": ("Phoenix Sky Harbor Intl", "Phoenix", "AZ", 33.4342, -112.0116),
    "LAS": ("Harry Reid Intl", "Las Vegas", "NV", 36.0840, -115.1537),
    "ABQ": ("Albuquerque Intl Sunport", "Albuquerque", "NM", 35.0402, -106.6090),
    "LAX": ("Los Angeles Intl", "Los Angeles", "CA", 33.9416, -118.4085),
    "SAN": ("San Diego Intl", "San Diego", "CA", 32.7338, -117.1933),
    "BWI": ("Baltimore/Washington Intl", "Baltimore", "MD", 39.1754, -76.6683),
    "MCO": ("Orlando Intl", "Orlando", "FL", 28.4312, -81.3081),
    "TPA": ("Tampa Intl", "Tampa", "FL", 27.9755, -82.5332),
}

# Focus cities: busier spokes that also get point-to-point flying and crew bases.
FOCUS_CITIES = ["HOU", "MDW", "DEN", "PHX", "LAS", "ATL", "BWI", "MCO", "BNA", "AUS"]

# Point-to-point (non-hub) routes. Every spoke is additionally connected to DAL.
POINT_TO_POINT = [
    ("HOU", "MSY"), ("HOU", "ATL"), ("HOU", "MDW"), ("HOU", "DEN"), ("HOU", "LAS"),
    ("HOU", "PHX"), ("HOU", "TPA"), ("HOU", "MCO"), ("AUS", "LAS"), ("AUS", "DEN"),
    ("AUS", "PHX"), ("SAT", "LAS"), ("SAT", "PHX"), ("MDW", "STL"), ("MDW", "BNA"),
    ("MDW", "DEN"), ("MDW", "LAS"), ("MDW", "MCO"), ("BNA", "ATL"), ("BNA", "MCO"),
    ("BNA", "BWI"), ("DEN", "PHX"), ("DEN", "LAS"), ("PHX", "LAX"), ("PHX", "SAN"),
    ("LAS", "LAX"), ("LAS", "SAN"), ("LAS", "ABQ"), ("PHX", "ELP"), ("PHX", "ABQ"),
    ("BWI", "MCO"), ("BWI", "TPA"), ("ATL", "MCO"), ("ATL", "TPA"), ("MCI", "DEN"),
    ("MCI", "PHX"), ("STL", "DEN"), ("OKC", "DEN"), ("TUL", "DEN"), ("MSY", "ATL"),
    ("MSY", "MCO"),
]

# Fleet mix: (type, seats, share of fleet, flight attendants required)
FLEET_MIX = [
    ("737-700", 143, 0.40, 3),
    ("737-800", 175, 0.25, 4),
    ("737 MAX 8", 175, 0.35, 4),
]

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Carlos", "Karen", "Daniel", "Lisa", "Matthew", "Nancy",
    "Anthony", "Sofia", "Mark", "Ashley", "Luis", "Emily", "Steven", "Maria",
    "Andrew", "Michelle", "Kevin", "Amanda", "Brian", "Priya", "Jose", "Grace",
    "Wei", "Aisha", "Diego", "Hannah", "Omar", "Olivia", "Ethan", "Camila",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Patel", "Kim", "Chen", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts",
]

# Loyalty tiers and their share of passengers.
TIERS = [("A-List Preferred", 0.03), ("A-List", 0.07), ("Member", 0.70), ("None", 0.20)]
