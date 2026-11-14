"""Stable fictional service names. Real POI names and brands are never used."""
import hashlib

VERSION = 1
PLACES = ('Brindlebrook', 'Coppervale', 'Driftwillow', 'Emberfen', 'Juniper Hollow',
          'Larkstone', 'Moonridge', 'Pebblecrest', 'Quillhaven', 'Sagewick',
          'Silverfern', 'Thistlebank', 'Willowmere', 'Wrenfield', 'Yarrowbend', 'Zephyr Glen')
KINDS = {
    'FOOD': ('Diner', 'Kitchen', 'Cafe', 'Grill'),
    'GAS': ('Fuel Stop', 'Travel Stop', 'Fuel & Go', 'Motor Stop'),
    'LODGING': ('Inn', 'Lodge', 'Motel', 'Rest House'),
    'CAMPING': ('Campground', 'Camp', 'RV Park', 'Camping'),
    'HOSPITAL': ('Medical Center', 'Hospital', 'Health Center', 'Community Hospital'),
}


def name(category, identity):
    digest = hashlib.sha256(f'service-fiction-v{VERSION}:{category}:{identity}'.encode()).digest()
    return PLACES[int.from_bytes(digest[:4], 'big') % len(PLACES)] + ' ' + KINDS[category][digest[4] % len(KINDS[category])]


def business(record):
    """Drop any supplied real identity before selecting or drawing a panel."""
    result = {k: v for k, v in record.items() if k not in ('name', 'brand', 'operator', 'verification')}
    result.update(name=name(record['category'], record['id']), name_origin='fictional',
                  provenance='inferred')
    return result
