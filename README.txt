ML Generator SR/CR
===================

Generate map sheet polygons for Slovakia and Czechia from an AOI polygon layer.

Basis
-----
- Bundled 1:5000 grids for SR and CR
- SR coarse scales follow UGKK SR rules
- Fine scales use recursive 2x2 subdivision: 1=NW, 2=NE, 3=SW, 4=SE

Workflow
--------
1. Choose country
2. Choose AOI polygon layer
3. Choose scale
4. Choose output CRS
5. Choose output: temporary layer or saved GPKG

Output fields
-------------
- sheet_name
- sheet_code
- parent_code
- scale
- country
- root_name
- base_5000
