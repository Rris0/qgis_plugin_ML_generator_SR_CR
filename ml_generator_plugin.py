
import os
import re
from collections import Counter, defaultdict

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget
)
try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtGui import QIcon

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

PLUGIN_NAME = "ML Generator SR/CR"
SCALE_ORDER = ["50000", "25000", "10000", "5000", "2000", "1000", "500", "250"]
FINE_SCALES = {"2000", "1000", "500", "250"}
COARSE_SCALES = {"50000", "25000", "10000"}
CELL_W = 2500.0
CELL_H = 2000.0
PARENT_W = CELL_W * 10.0
PARENT_H = CELL_H * 10.0
NAME_PARSE_RE = re.compile(r"^(?P<root>.+?)[ _](?P<col>\d+)-(?P<row>\d+)(?:\.tif)?$", re.IGNORECASE)


def dialog_button_flags():
    try:
        return QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    except AttributeError:
        return QDialogButtonBox.Ok | QDialogButtonBox.Cancel


def dialog_exec(dialog):
    try:
        return dialog.exec()
    except AttributeError:
        return dialog.exec_()


class ConfigDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Generate Map Sheets")
        self.resize(520, 300)

        self.country_combo = QComboBox()
        self.country_combo.addItems(["Slovakia", "Czechia"])

        self.layer_combo = QComboBox()
        self.refresh_layers_btn = QPushButton("Refresh")
        self.refresh_layers_btn.clicked.connect(self.refresh_layers)

        self.selected_only_chk = QCheckBox("Use selected polygons only")
        self.selected_only_chk.setChecked(True)

        self.scale_combo = QComboBox()
        self.scale_combo.addItems([f"1:{s}" for s in SCALE_ORDER])

        self.output_crs_combo = QComboBox()
        self.output_crs_combo.addItems(["AOI layer CRS", "Project CRS", "Grid CRS (EPSG:5514)"])

        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItems(["Temporary layer", "Save to folder (GPKG)"])
        self.output_mode_combo.currentIndexChanged.connect(self._update_output_widgets)

        self.output_folder_edit = QLineEdit()
        self.output_folder_btn = QPushButton("Browse")
        self.output_folder_btn.clicked.connect(self.browse_output_folder)

        layer_row = QHBoxLayout()
        layer_row.addWidget(self.layer_combo, 1)
        layer_row.addWidget(self.refresh_layers_btn)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self.output_folder_edit, 1)
        folder_row.addWidget(self.output_folder_btn)

        form = QFormLayout()
        form.addRow("Country", self.country_combo)
        form.addRow("AOI layer", self._wrap(layer_row))
        form.addRow("", self.selected_only_chk)
        form.addRow("Scale", self.scale_combo)
        form.addRow("Output CRS", self.output_crs_combo)
        form.addRow("Output", self.output_mode_combo)
        form.addRow("Folder", self._wrap(folder_row))

        info = QLabel(
            "Uses bundled 1:5000 grids. "
            "SR coarse scales follow UGKK SR. "
            "Fine scales use 2x2 subdivision (1=NW, 2=NE, 3=SW, 4=SE)."
        )
        info.setWordWrap(True)

        buttons = QDialogButtonBox(dialog_button_flags())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(info)
        layout.addWidget(buttons)
        self.refresh_layers()
        self._update_output_widgets()

    def _wrap(self, layout):
        w = QWidget()
        w.setLayout(layout)
        return w

    def _update_output_widgets(self):
        enabled = self.output_mode_combo.currentText().startswith("Save")
        self.output_folder_edit.setEnabled(enabled)
        self.output_folder_btn.setEnabled(enabled)

    def browse_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self.output_folder_edit.text() or "")
        if folder:
            self.output_folder_edit.setText(folder)

    def refresh_layers(self):
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                self.layer_combo.addItem(layer.name(), layer.id())

    def selected_layer(self):
        lid = self.layer_combo.currentData()
        return QgsProject.instance().mapLayer(lid)


class MLGeneratorPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dataset_cache = {}

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icons", "icon.svg"))
        self.action = QAction(icon, PLUGIN_NAME, self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&ML Generator SR/CR", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&ML Generator SR/CR", self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        dlg = ConfigDialog(self.iface, self.iface.mainWindow())
        if not dialog_exec(dlg):
            return
        aoi_layer = dlg.selected_layer()
        if not aoi_layer:
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_NAME, "Please select an AOI polygon layer.")
            return

        save_to_folder = dlg.output_mode_combo.currentText().startswith("Save")
        output_folder = dlg.output_folder_edit.text().strip()
        if save_to_folder and not output_folder:
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_NAME, "Please select an output folder.")
            return
        if save_to_folder and not os.path.isdir(output_folder):
            QMessageBox.warning(self.iface.mainWindow(), PLUGIN_NAME, "Output folder does not exist.")
            return

        country = "SR" if dlg.country_combo.currentText() == "Slovakia" else "CR"
        target_scale = dlg.scale_combo.currentText().replace("1:", "")
        use_selected = dlg.selected_only_chk.isChecked()
        output_crs_mode = dlg.output_crs_combo.currentText()

        try:
            result_layer = self.generate(country, aoi_layer, target_scale, use_selected, output_crs_mode)
            if save_to_folder:
                result_layer = self.save_layer(result_layer, output_folder, country, target_scale)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), PLUGIN_NAME, f"Generation failed:\n{exc}")
            raise

        QgsProject.instance().addMapLayer(result_layer)
        mode_text = "saved" if save_to_folder else "generated"
        QMessageBox.information(self.iface.mainWindow(), PLUGIN_NAME, f"{result_layer.featureCount()} map sheets {mode_text} at 1:{target_scale}.")

    def data_path(self, country):
        return os.path.join(os.path.dirname(__file__), "data", "sr_5000.gpkg" if country == "SR" else "cr_5000.gpkg")

    def optional_50000_path(self, country):
        name = "sr_50000.gpkg" if country == "SR" else "cr_50000.gpkg"
        path = os.path.join(os.path.dirname(__file__), "data", name)
        return path if os.path.exists(path) else None

    def load_optional_50000_layer(self, country):
        path = self.optional_50000_path(country)
        if not path:
            return None
        layer = QgsVectorLayer(f"{path}|layername=grid", f"{country}_50000", "ogr")
        return layer if layer.isValid() else None

    @staticmethod
    def detect_name_field(layer, candidates):
        if not layer:
            return None
        names = [f.name() for f in layer.fields()]
        lower = {n.lower(): n for n in names}
        for cand in candidates:
            if cand in names:
                return cand
            hit = lower.get(cand.lower())
            if hit:
                return hit
        return None

    def load_dataset(self, country):
        cached = self.dataset_cache.get(country)
        if cached:
            return cached

        path = self.data_path(country)
        layer = QgsVectorLayer(f"{path}|layername=grid", f"{country}_5000", "ogr")
        if not layer.isValid():
            raise RuntimeError(f"Bundled dataset could not be loaded: {path}")

        field_names = [f.name() for f in layer.fields()]
        name_field = "Text" if country == "SR" else "MAPNAME"
        nom_field = None if country == "SR" else ("MAPNOM" if "MAPNOM" in field_names else None)
        if name_field not in field_names:
            raise RuntimeError(f"Expected field '{name_field}' not found in bundled dataset for {country}.")

        layer_50000 = self.load_optional_50000_layer(country)
        idx_50000 = QgsSpatialIndex(layer_50000.getFeatures()) if layer_50000 else None
        name_field_50000 = self.detect_name_field(
            layer_50000,
            (["MAPNAME", "Text", "NAME", "NAZOV", "NOMENKLATURA", "OZNACENI", "MAPNOM"] if country == "CR" else ["MAPNOM", "MAPNAME", "Text", "NAME", "NAZOV", "NOMENKLATURA", "OZNACENI"])
        )
        code_field_50000 = self.detect_name_field(layer_50000, ["MAPNOM", "CODE", "KOD", "OZNACENI"])

        feats_50000 = {}
        if layer_50000:
            for feat50 in layer_50000.getFeatures():
                raw_name = str(feat50[name_field_50000]).strip() if name_field_50000 else ""
                raw_code = str(feat50[code_field_50000]).strip() if code_field_50000 else ""
                display_name = raw_name
                if country == "CR" and raw_name and raw_code and raw_name.startswith(raw_code):
                    display_name = raw_name[len(raw_code):].strip()
                feats_50000[feat50.id()] = {
                    "geom": QgsGeometry(feat50.geometry()),
                    "name": display_name,
                    "code": raw_code or raw_name,
                }

        index = QgsSpatialIndex(layer.getFeatures())
        feature_payload = {}
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            bbox = geom.boundingBox()
            parsed = self.parse_5000_name(str(feat[name_field]).strip(), str(feat[nom_field]).strip() if nom_field else "", country)
            if not parsed:
                continue
            payload = {"fid": feat.id(), "bbox": bbox, "geom": QgsGeometry(geom), **parsed}
            payload["parent_key"] = self.parent_key_from_bbox_and_indices(bbox, payload["col_num"], payload["row_num"])

            if idx_50000:
                cx = (bbox.xMinimum() + bbox.xMaximum()) / 2.0
                cy = (bbox.yMinimum() + bbox.yMaximum()) / 2.0
                center_rect = QgsRectangle(cx, cy, cx, cy)
                for fid50 in idx_50000.intersects(center_rect):
                    f50 = feats_50000.get(fid50)
                    if f50 and f50["geom"].intersects(QgsGeometry.fromRect(center_rect)):
                        coords = f50["geom"].boundingBox().toRectF().getCoords()
                        payload["parent_key"] = tuple(round(v) for v in coords)
                        payload["parent50_name"] = f50["name"] or payload["root_display"]
                        payload["parent50_code"] = f50["code"] or f50["name"] or payload["root_nom"] or payload["root_display"]
                        break
            feature_payload[feat.id()] = payload

        parent_groups = defaultdict(list)
        for fid, payload in feature_payload.items():
            parent_groups[payload["parent_key"]].append(fid)

        parent_name, parent_nom, parent50_name, parent50_code = {}, {}, {}, {}
        for pkey, fids in parent_groups.items():
            by_display = Counter(feature_payload[fid]["root_display"] for fid in fids)
            root_display = sorted(by_display.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            parent_name[pkey] = root_display

            by_nom = Counter(feature_payload[fid]["root_nom"] for fid in fids if feature_payload[fid]["root_nom"])
            parent_nom[pkey] = sorted(by_nom.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if by_nom else ""

            by_50_code = Counter(feature_payload[fid].get("parent50_code", "") for fid in fids if feature_payload[fid].get("parent50_code", ""))
            by_50_name = Counter(feature_payload[fid].get("parent50_name", "") for fid in fids if feature_payload[fid].get("parent50_name", ""))
            parent50_code[pkey] = sorted(by_50_code.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if by_50_code else parent_nom[pkey] or root_display
            parent50_name[pkey] = sorted(by_50_name.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if by_50_name else root_display

        data = {
            "layer": layer,
            "index": index,
            "feature_payload": feature_payload,
            "parent_groups": dict(parent_groups),
            "parent_name": parent_name,
            "parent_nom": parent_nom,
            "parent50_name": parent50_name,
            "parent50_code": parent50_code,
            "crs": layer.crs(),
            "country": country,
        }
        self.dataset_cache[country] = data
        return data

    @staticmethod
    def parent_key_from_bbox_and_indices(bbox, col_num, row_num):
        parent_minx = bbox.xMinimum() - ((9 - col_num) * CELL_W)
        parent_maxx = parent_minx + PARENT_W
        parent_maxy = bbox.yMaximum() + (row_num * CELL_H)
        parent_miny = parent_maxy - PARENT_H
        return (round(parent_minx), round(parent_miny), round(parent_maxx), round(parent_maxy))

    @staticmethod
    def parse_5000_name(full_name, nom_value, country):
        m = NAME_PARSE_RE.match(full_name.strip())
        if not m:
            return None
        root_raw = m.group("root")
        col_num = int(m.group("col"))
        row_num = int(m.group("row"))
        root_display = root_raw.replace("_", " ").strip()
        root_nom = ""
        if country == "CR" and nom_value:
            md = re.match(r"^(.*?)(\d+)(\d+)$", nom_value)
            if md:
                root_nom = md.group(1)
        return {
            "full_name": full_name.replace("_", " "),
            "root_display": root_display,
            "root_key": root_raw.strip(),
            "root_nom": root_nom,
            "col_num": col_num,
            "row_num": row_num,
            "code": f"{col_num}-{row_num}",
        }

    @staticmethod
    def combined_aoi(aoi_layer, use_selected):
        feats = list(aoi_layer.selectedFeatures()) if use_selected and aoi_layer.selectedFeatureCount() > 0 else list(aoi_layer.getFeatures())
        if not feats:
            raise RuntimeError("AOI layer has no features to use.")
        geom = None
        for feat in feats:
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            geom = QgsGeometry(g) if geom is None else geom.combine(g)
        if geom is None or geom.isEmpty():
            raise RuntimeError("AOI geometry is empty.")
        return geom

    def generate(self, country, aoi_layer, target_scale, use_selected, output_crs_mode):
        data = self.load_dataset(country)
        grid_crs = data["crs"]
        aoi_geom = self.combined_aoi(aoi_layer, use_selected)
        aoi_grid = QgsGeometry(aoi_geom)
        if aoi_layer.crs() != grid_crs:
            tr = QgsCoordinateTransform(aoi_layer.crs(), grid_crs, QgsProject.instance())
            aoi_grid.transform(tr)

        base_cells = self.base_cells_intersecting(data, aoi_grid)
        if not base_cells:
            raise RuntimeError("AOI does not intersect any bundled 1:5000 map sheets for the selected country.")

        if target_scale == "5000":
            records = self.generate_5000_records(base_cells, country)
        elif target_scale in COARSE_SCALES:
            records = self.generate_coarse_records(base_cells, target_scale, data, aoi_grid)
        elif target_scale in FINE_SCALES:
            records = self.generate_fine_records(base_cells, target_scale, aoi_grid, country)
        else:
            raise RuntimeError(f"Unsupported scale: 1:{target_scale}")
        if not records:
            raise RuntimeError("No map sheets were generated for the selected AOI and scale.")

        out_crs = aoi_layer.crs() if output_crs_mode == "AOI layer CRS" else (QgsProject.instance().crs() if output_crs_mode == "Project CRS" else grid_crs)
        return self.records_to_layer(records, grid_crs, out_crs, target_scale, country)

    @staticmethod
    def base_cells_intersecting(data, aoi_grid):
        hits = []
        for fid in data["index"].intersects(aoi_grid.boundingBox()):
            payload = data["feature_payload"].get(fid)
            if payload and payload["geom"].intersects(aoi_grid):
                hits.append(payload)
        return hits

    @staticmethod
    def rect_geom(xmin, ymin, xmax, ymax):
        return QgsGeometry.fromRect(QgsRectangle(round(xmin), round(ymin), round(xmax), round(ymax)))

    @staticmethod
    def generate_5000_records(base_cells, country):
        return [{
            "geom": QgsGeometry(cell["geom"]),
            "sheet_name": cell["full_name"],
            "sheet_code": cell["code"],
            "parent_code": cell["root_display"],
            "base_5000_name": cell["full_name"],
            "source_scale": "5000",
            "root_name": cell["root_display"],
            "country": country,
        } for cell in base_cells]

    def generate_coarse_records(self, base_cells, target_scale, data, aoi_grid):
        if data["country"] == "SR":
            return self.generate_coarse_records_sr(base_cells, target_scale, data, aoi_grid)
        return self.generate_coarse_records_cr(base_cells, target_scale, data, aoi_grid)

    def generate_coarse_records_sr(self, base_cells, target_scale, data, aoi_grid):
        groups = {}
        parent_names = data.get("parent50_name") or data["parent_name"]
        for cell in base_cells:
            xmin50, ymin50, xmax50, ymax50 = cell["parent_key"]
            root_name = parent_names.get(cell["parent_key"], cell["root_display"])
            col, row = cell["col_num"], cell["row_num"]

            if target_scale == "50000":
                key = (xmin50, ymin50, xmax50, ymax50, "50000", root_name)
                groups[key] = self.make_group_item(xmin50, ymin50, xmax50, ymax50, root_name, root_name, "", root_name, cell["full_name"])
                continue

            if target_scale == "25000":
                north_half = row <= 4
                west_half = col >= 5
                if north_half:
                    quad = "1" if west_half else "2"
                    rxmin, rxmax = (xmin50, xmin50 + 12500.0) if west_half else (xmin50 + 12500.0, xmax50)
                    rymin, rymax = ymax50 - 10000.0, ymax50
                else:
                    quad = "3" if west_half else "4"
                    rxmin, rxmax = (xmin50, xmin50 + 12500.0) if west_half else (xmin50 + 12500.0, xmax50)
                    rymin, rymax = ymin50, ymin50 + 10000.0
                sheet_name = f"{root_name} {quad}".strip()
                key = (rxmin, rymin, rxmax, rymax, "25000", quad, root_name)
                groups[key] = self.make_group_item(rxmin, rymin, rxmax, rymax, sheet_name, quad, root_name, root_name, cell["full_name"])
                continue

            block_col_from_west = (9 - col) // 2
            block_row_from_north = row // 2
            num = block_row_from_north * 5 + block_col_from_west + 1
            rxmin = xmin50 + (block_col_from_west * 5000.0)
            rxmax = rxmin + 5000.0
            rymax = ymax50 - (block_row_from_north * 4000.0)
            rymin = rymax - 4000.0
            code = f"{num:02d}"
            sheet_name = f"{root_name} {code}".strip()
            key = (rxmin, rymin, rxmax, rymax, "10000", code, root_name)
            groups[key] = self.make_group_item(rxmin, rymin, rxmax, rymax, sheet_name, code, root_name, root_name, cell["full_name"])

        return self._groups_to_records(groups, aoi_grid, target_scale, "SR")

    def generate_coarse_records_cr(self, base_cells, target_scale, data, aoi_grid):
        groups = {}
        parent_names = data.get("parent50_name") or data["parent_name"]
        parent_codes = data.get("parent50_code") or data["parent_nom"]
        for cell in base_cells:
            xmin50, ymin50, xmax50, ymax50 = cell["parent_key"]
            root_name = parent_names.get(cell["parent_key"], cell["root_display"])
            root_code = parent_codes.get(cell["parent_key"], cell.get("root_nom", "") or root_name)
            col, row = cell["col_num"], cell["row_num"]
            west_half = col >= 5
            north_half = row <= 4
            quad = "a" if (north_half and west_half) else "b" if (north_half and not west_half) else "c" if (not north_half and west_half) else "d"

            if target_scale == "50000":
                key = (xmin50, ymin50, xmax50, ymax50, "50000", root_code)
                full_label = f"{root_code} {root_name}".strip() if root_name else root_code
                groups[key] = self.make_group_item(xmin50, ymin50, xmax50, ymax50, full_label, root_code, "", root_name, cell["full_name"])
                continue

            if target_scale == "25000":
                rxmin, rxmax = (xmin50, xmin50 + 12500.0) if west_half else (xmin50 + 12500.0, xmax50)
                rymin, rymax = ((ymax50 - 10000.0, ymax50) if north_half else (ymin50, ymin50 + 10000.0))
                code = f"{root_code} {quad}".strip()
                full_label = f"{code} {root_name}".strip() if root_name else code
                key = (rxmin, rymin, rxmax, rymax, "25000", code)
                groups[key] = self.make_group_item(rxmin, rymin, rxmax, rymax, full_label, code, root_code, root_name, cell["full_name"])
                continue

            block_col_from_west = (9 - col) // 2
            block_row_from_north = row // 2
            num = block_row_from_north * 5 + block_col_from_west + 1
            rxmin = xmin50 + (block_col_from_west * 5000.0)
            rxmax = rxmin + 5000.0
            rymax = ymax50 - (block_row_from_north * 4000.0)
            rymin = rymax - 4000.0
            code = f"{root_code} {quad} {num:02d}".strip()
            full_label = f"{code} {root_name}".strip() if root_name else code
            key = (rxmin, rymin, rxmax, rymax, "10000", code)
            groups[key] = self.make_group_item(rxmin, rymin, rxmax, rymax, full_label, code, f"{root_code} {quad}".strip(), root_name, cell["full_name"])

        return self._groups_to_records(groups, aoi_grid, target_scale, "CR")

    def make_group_item(self, xmin, ymin, xmax, ymax, sheet_name, sheet_code, parent_code, root_name, base_5000_name):
        return {
            "geom": self.rect_geom(xmin, ymin, xmax, ymax),
            "sheet_name": sheet_name,
            "sheet_code": sheet_code,
            "parent_code": parent_code,
            "root_name": root_name,
            "base_5000_name": base_5000_name,
        }

    @staticmethod
    def _groups_to_records(groups, aoi_grid, target_scale, country):
        records, seen = [], set()
        for item in groups.values():
            if not item["geom"].intersects(aoi_grid):
                continue
            bbox = item["geom"].boundingBox()
            sig = (round(bbox.xMinimum()), round(bbox.yMinimum()), round(bbox.xMaximum()), round(bbox.yMaximum()), item["sheet_code"])
            if sig in seen:
                continue
            seen.add(sig)
            records.append({
                "geom": item["geom"],
                "sheet_name": item["sheet_name"],
                "sheet_code": item["sheet_code"],
                "parent_code": item["parent_code"],
                "base_5000_name": item["base_5000_name"],
                "source_scale": target_scale,
                "root_name": item["root_name"],
                "country": country,
            })
        return records

    @staticmethod
    def fine_steps(target_scale):
        order = ["5000", "2000", "1000", "500", "250"]
        return order[1:order.index(target_scale) + 1]

    @staticmethod
    def quadrant_parts(bbox):
        xmin, xmax = bbox.xMinimum(), bbox.xMaximum()
        ymin, ymax = bbox.yMinimum(), bbox.yMaximum()
        xmid = (xmin + xmax) / 2.0
        ymid = (ymin + ymax) / 2.0
        return {
            "1": QgsRectangle(xmin, ymid, xmid, ymax),
            "2": QgsRectangle(xmid, ymid, xmax, ymax),
            "3": QgsRectangle(xmin, ymin, xmid, ymid),
            "4": QgsRectangle(xmid, ymin, xmax, ymid),
        }

    def generate_fine_records(self, base_cells, target_scale, aoi_grid, country):
        records = []
        steps = self.fine_steps(target_scale)
        for cell in base_cells:
            current = [{
                "rect": cell["bbox"],
                "code": cell["code"],
                "root_name": cell["root_display"],
                "full_name": cell["full_name"],
                "parent_code": cell["code"],
            }]
            for _ in steps:
                next_level = []
                for item in current:
                    for quad_digit, qrect in self.quadrant_parts(item["rect"]).items():
                        qgeom = QgsGeometry.fromRect(qrect)
                        if not qgeom.intersects(aoi_grid):
                            continue
                        new_code = f"{item['code']}{quad_digit}"
                        next_level.append({
                            "rect": qrect,
                            "code": new_code,
                            "root_name": item["root_name"],
                            "full_name": f"{item['root_name']} {new_code}",
                            "parent_code": item["code"],
                        })
                current = next_level
            for item in current:
                geom = QgsGeometry.fromRect(item["rect"])
                if not geom.intersects(aoi_grid):
                    continue
                records.append({
                    "geom": geom,
                    "sheet_name": item["full_name"],
                    "sheet_code": item["code"],
                    "parent_code": item["parent_code"],
                    "base_5000_name": cell["full_name"],
                    "source_scale": target_scale,
                    "root_name": item["root_name"],
                    "country": country,
                })
        return records

    def records_to_layer(self, records, src_crs, out_crs, target_scale, country):
        layer = QgsVectorLayer(f"Polygon?crs={out_crs.authid()}", f"ML_{country}_{target_scale}", "memory")
        pr = layer.dataProvider()
        fields = QgsFields()
        for name, length in (("sheet_name", 255), ("sheet_code", 64), ("parent_code", 64), ("scale", 16), ("country", 8), ("root_name", 255), ("base_5000", 255)):
            fields.append(QgsField(name, QVariant.String, len=length))
        pr.addAttributes(fields)
        layer.updateFields()

        transformer = QgsCoordinateTransform(src_crs, out_crs, QgsProject.instance()) if src_crs != out_crs else None
        feats = []
        for rec in records:
            feat = QgsFeature(layer.fields())
            geom = QgsGeometry(rec["geom"])
            if transformer:
                geom.transform(transformer)
            feat.setGeometry(geom)
            feat["sheet_name"] = rec["sheet_name"]
            feat["sheet_code"] = rec["sheet_code"]
            feat["parent_code"] = rec["parent_code"]
            feat["scale"] = f"1:{target_scale}"
            feat["country"] = country
            feat["root_name"] = rec["root_name"]
            feat["base_5000"] = rec["base_5000_name"]
            feats.append(feat)
        pr.addFeatures(feats)
        layer.updateExtents()
        return layer

    def save_layer(self, layer, output_folder, country, target_scale):
        file_name = f"ML_{country}_{target_scale}.gpkg"
        path = os.path.join(output_folder, file_name)
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = f"ML_{country}_{target_scale}"
        options.fileEncoding = "UTF-8"
        try:
            writer = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsProject.instance().transformContext(), options)
        except AttributeError:
            writer = QgsVectorFileWriter.writeAsVectorFormat(layer, path, "UTF-8", layer.crs(), "GPKG")
        if isinstance(writer, tuple):
            err = writer[0]
        else:
            err = writer
        if err != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"Could not save output: {path}")
        saved = QgsVectorLayer(f"{path}|layername=ML_{country}_{target_scale}", f"ML_{country}_{target_scale}", "ogr")
        if not saved.isValid():
            raise RuntimeError(f"Saved layer could not be reloaded: {path}")
        return saved
