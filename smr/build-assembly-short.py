#!/usr/bin/env python3

import argparse
import copy
from pathlib import Path

import numpy as np
from tqdm import tqdm

import openmc
from smr.materials import materials, mats
from smr.surfaces import surfs, lattice_pitch, pin_pitch, bottom_fuel_stack, \
    top_active_core
from smr.pins import pin_universes


# Define command-line options
parser = argparse.ArgumentParser()
parser.add_argument('-m', '--multipole', action='store_true',
                    help='Whether to use multipole cross sections')
parser.add_argument('-t', '--tallies', choices=('cell', 'mat'), default='mat',
                    help='Whether to use distribmats or distribcells for tallies')
parser.add_argument('-a', '--axial', type=int, default=196,
                    help='Number of axial subdivisions in fuel')
parser.add_argument('-d', '--depleted', action='store_true',
                    help='Whether UO2 compositions should represent depleted fuel')
parser.add_argument('-o', '--output-dir', type=Path, default=None)
args = parser.parse_args()

# Make directory for inputs
if args.output_dir is None:
    if args.depleted:
        directory = Path('assembly-short-depleted')
    else:
        directory = Path('assembly-short-fresh')
else:
    directory = args.output_dir
directory.mkdir(exist_ok=True)

rings = [0.1*pin_pitch, 0.2*pin_pitch]

# Define the NumPy array indices for assembly locations where there
# may be CR guide tubes, instrument tubes and burnable absorbers
nonfuel_y = np.array([2,2,2,3,3,5,5,5,5,5,8,8,8,8,8,11,11,11,11,11,13,13,14,14,14])
nonfuel_x = np.array([5,8,11,3,13,2,5,8,11,14,2,5,8,11,14,2,5,8,11,14,3,13,5,8,11])

# NO BURNABLE ABSORBERS
pins = pin_universes(rings, args.axial, args.depleted)
gtu = pins['GT empty']
#gti = pins['GT empty instr']
universes = np.empty((17,17), dtype=openmc.Universe)
universes[:,:] = pins['Fuel pin (3.1%) no grid']
universes[nonfuel_y, nonfuel_x] = [    gtu,   gtu,   gtu,
                                     gtu,              gtu,
                                   gtu, gtu,  gtu,  gtu, gtu,
                                   gtu, gtu,  gtu,  gtu, gtu,
                                   gtu, gtu,  gtu,  gtu, gtu,
                                     gtu,              gtu,
                                       gtu,   gtu,   gtu     ]

xy_bounds = openmc.model.get_rectangular_prism(
    lattice_pitch, lattice_pitch, boundary_type='reflective')
z_bounds = +surfs['bot active core'] & -surfs['top active core']
surfs['bot active core'].boundary_type = 'reflective'
surfs['top active core'].boundary_type = 'reflective'

# Instantiate the lattice
lattice = openmc.RectLattice(name='Pin lattice')
lattice.lower_left = (-17.*pin_pitch/2., -17.*pin_pitch/2.)
lattice.pitch = (pin_pitch, pin_pitch)
lattice.universes = universes

# Add lattice to bounding cell
root_universe = openmc.Universe(name='Root universe')
cell = openmc.Cell(name='Lattice cell')
cell.fill = lattice
cell.region = surfs['lat grid box inner'] & z_bounds
root_universe.add_cell(cell)

# Add outer water cell
cell = openmc.Cell(name='outer water')
cell.fill = mats['H2O']
cell.region = ~surfs['lat grid box inner'] & xy_bounds & z_bounds
root_universe.add_cell(cell)

# Define geometry with a single assembly
geometry = openmc.Geometry(root_universe)


def clone(material):
    """Perform copy of material but share nuclide densities"""
    shared_mat = copy.copy(material)
    shared_mat.id = None
    return shared_mat


#### "Differentiate" the geometry if using distribmats
if args.tallies == 'mat':
    # Count the number of instances for each cell and material
    geometry.determine_paths(instances_only=True)

    # Extract all cells filled by a fuel material
    fuel_mats = {m for m in materials if 'UO2 Fuel' in m.name}

    for cell in tqdm(geometry.get_all_material_cells().values(),
                     desc='Differentiating materials'):
        if cell.fill in fuel_mats:
            # Fill cell with list of "differentiated" materials
            cell.fill = [clone(cell.fill) for i in range(cell.num_instances)]

#### Create OpenMC "materials.xml" file
print('Getting materials...')
all_materials = geometry.get_all_materials()
print('Creating materials collection...')
materials = openmc.Materials(all_materials.values())
print('Exporting materials to XML...')
materials.export_to_xml(str(directory / 'materials.xml'))


#### Create OpenMC "geometry.xml" file
geometry.export_to_xml(str(directory / 'geometry.xml'))


#### Create OpenMC "settings.xml" file

# Construct uniform initial source distribution over fissionable zones
lower_left = (-lattice_pitch/2, -lattice_pitch/2, bottom_fuel_stack)
upper_right = (lattice_pitch/2, lattice_pitch/2, top_active_core)
source = openmc.source.Source(space=openmc.stats.Box(lower_left, upper_right))
source.space.only_fissionable = True

settings = openmc.Settings()
settings.batches = 200
settings.inactive = 100
settings.particles = 10000
settings.output = {'tallies': False, 'summary': False}
settings.source = source
settings.sourcepoint_write = False

if args.multipole:
    settings.temperature = {'multipole': True, 'tolerance': 1000}

settings.export_to_xml(str(directory / 'settings.xml'))


####  Create OpenMC "tallies.xml" file
tallies = openmc.Tallies()

# Extract all fuel materials
materials = geometry.get_materials_by_name(name='Fuel', matching=False)

# If using distribcells, create distribcell tally needed for depletion
if args.tallies == 'cell':
    # Extract all cells filled by a fuel material
    fuel_cells = []
    for cell in geometry.get_all_cells().values():
        if cell.fill in materials:
            tally = openmc.Tally(name='depletion tally')
            tally.scores = ['(n,p)', '(n,a)', '(n,gamma)',
                            'fission', '(n,2n)', '(n,3n)', '(n,4n)']
            tally.nuclides = cell.fill.get_nuclides()
            tally.filters.append(openmc.DistribcellFilter([cell]))
            tallies.append(tally)

# If using distribmats, create material tally needed for depletion
elif args.tallies == 'mat':
    tally = openmc.Tally(name='depletion tally')
    tally.scores = ['(n,p)', '(n,a)', '(n,gamma)',
                    'fission', '(n,2n)', '(n,3n)', '(n,4n)']
    tally.nuclides = materials[0].get_nuclides()
    tally.filters = [openmc.MaterialFilter(materials)]
    tallies.append(tally)

tallies.export_to_xml(str(directory / 'tallies.xml'))