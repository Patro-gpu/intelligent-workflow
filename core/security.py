"""工具权限层物理安全模块。

提供：
1. `resolve_safe_path`：强制 `.resolve()` 路径规范化，防止 `../` 路径穿越与符号链接逃逸。
2. `sanitize_cell_for_export`：电子表格导出公式注入防护（转义 `=`, `+`, `-`, `@`, `\t`, `\r`）。
3. `DatasetRegistry`：只读 CSV 加载、大小与行列硬上限、安全列 ID 映射（`col_01`...）、
   恶意表头隔离与 `NaN`/`Inf`/`-Inf` 非有限数值预警。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.schema import ColumnMapping, DatasetProfile


class SecurityError(Exception):
    """权限或输入安全违规异常。"""


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")
_PROMPT_INJECTION_HINTS = (
    "ignore previous",
    "system prompt",
    "read .env",
    "api_key",
    "secret",
)


def resolve_safe_path(
    path: str | Path,
    allowed_roots: Sequence[str | Path],
) -> Path:
    """规范化真实路径并校验其严格位于 `allowed_roots` 内，否则抛出 `SecurityError`。"""
    if not allowed_roots:
        raise SecurityError("allowed_roots must not be empty")

    candidate = Path(path).expanduser().resolve()
    resolved_roots = [Path(root).expanduser().resolve() for root in allowed_roots]

    for root in resolved_roots:
        try:
            if candidate == root or candidate.is_relative_to(root):
                return candidate
        except AttributeError:
            # Fallback if ever run on an older runtime without Path.is_relative_to
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue

    raise SecurityError(
        f"Path '{path}' resolves to '{candidate}', which is outside allowed roots."
    )


def sanitize_cell_for_export(value: Any) -> Any:
    """防止电子表格公式注入：以危险前缀开头的字符串添加单引号转义。"""
    if not isinstance(value, str):
        return value
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def sanitize_column_name(raw_name: Any, max_len: int = 48) -> str:
    """清洗原始列名供人类展示：剥离换行与控制字符并截断长度。"""
    text = str(raw_name)
    text = _CONTROL_CHAR_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "unnamed_col"
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _infer_role_hint(series: pd.Series, sanitized_name: str) -> str:
    lowered = sanitized_name.lower()
    if lowered.endswith("_id") or lowered == "id" or "merchant" in lowered:
        return "id"
    if pd.api.types.is_bool_dtype(series):
        return "group"
    if pd.api.types.is_numeric_dtype(series):
        finite = series.replace([np.inf, -np.inf], np.nan).dropna()
        unique_count = int(finite.nunique())
        if 2 <= unique_count <= 5 and set(finite.unique()).issubset({0, 1, 0.0, 1.0}):
            return "group"
        return "numeric_metric"
    unique_count = int(series.dropna().nunique())
    if 2 <= unique_count <= 10:
        return "group"
    return "text"


class DatasetRegistry:
    """只读 CSV 数据集注册表，隔离原始路径与恶意列名。"""

    def __init__(
        self,
        allowed_roots: Sequence[str | Path],
        max_file_size_mb: float = 10.0,
        max_rows: int = 100_000,
        max_cols: int = 100,
    ) -> None:
        if not allowed_roots:
            raise SecurityError("DatasetRegistry requires at least one allowed root")
        self.allowed_roots: List[Path] = [Path(r).expanduser().resolve() for r in allowed_roots]
        self.max_file_size_bytes: int = int(max_file_size_mb * 1024 * 1024)
        self.max_rows: int = max_rows
        self.max_cols: int = max_cols

        self._profiles: Dict[str, DatasetProfile] = {}
        self._dataframes: Dict[str, pd.DataFrame] = {}
        self._col_maps: Dict[str, Dict[str, ColumnMapping]] = {}

    def register_csv(
        self,
        file_path: str | Path,
        dataset_id: Optional[str] = None,
    ) -> DatasetProfile:
        """安全校验、只读加载 CSV 并注册内部安全列 ID（`col_01`...）。"""
        safe_path = resolve_safe_path(file_path, self.allowed_roots)
        if not safe_path.exists() or not safe_path.is_file():
            raise SecurityError(f"CSV file does not exist or is not a regular file: {safe_path}")

        file_size = safe_path.stat().st_size
        if file_size > self.max_file_size_bytes:
            raise SecurityError(
                f"File size {file_size} bytes exceeds limit of {self.max_file_size_bytes} bytes"
            )

        raw_bytes = safe_path.read_bytes()
        dataset_hash = hashlib.sha256(raw_bytes).hexdigest()

        df = pd.read_csv(safe_path)
        row_count, col_count = df.shape
        if row_count > self.max_rows:
            raise SecurityError(f"Row count {row_count} exceeds limit of {self.max_rows}")
        if col_count > self.max_cols:
            raise SecurityError(f"Column count {col_count} exceeds limit of {self.max_cols}")

        assigned_id = dataset_id or f"ds_{dataset_hash[:12]}"
        columns: List[ColumnMapping] = []
        col_map: Dict[str, ColumnMapping] = {}
        rename_dict: Dict[Any, str] = {}
        quality_warnings: List[str] = []
        candidate_group_cols: List[str] = []

        for idx, raw_col in enumerate(df.columns, start=1):
            col_id = f"col_{idx:02d}"
            raw_col_str = str(raw_col)
            raw_name_hash = hashlib.sha256(raw_col_str.encode("utf-8")).hexdigest()
            sanitized_name = sanitize_column_name(raw_col_str)
            series = df.iloc[:, idx - 1]
            role_hint = _infer_role_hint(series, sanitized_name)

            mapping = ColumnMapping(
                col_id=col_id,
                sanitized_name=sanitized_name,
                raw_name_hash=raw_name_hash,
                dtype=str(series.dtype),
                role_hint=role_hint,
            )
            columns.append(mapping)
            col_map[col_id] = mapping
            rename_dict[raw_col] = col_id

            if role_hint == "group":
                candidate_group_cols.append(col_id)

            # Check suspicious prompt-injection / formula headers
            lowered_header = raw_col_str.lower()
            if any(hint in lowered_header for hint in _PROMPT_INJECTION_HINTS) or raw_col_str.startswith(
                _FORMULA_PREFIXES
            ):
                quality_warnings.append(
                    f"{col_id}: suspicious header isolated via safe col_id mapping"
                )

            # Check NaN and +/-Inf values
            nan_count = int(series.isna().sum())
            if nan_count > 0:
                quality_warnings.append(f"{col_id}: contains {nan_count} NaN value(s)")

            if pd.api.types.is_numeric_dtype(series):
                inf_count = int(np.isinf(series.to_numpy(dtype=float, na_value=np.nan)).sum())
                if inf_count > 0:
                    quality_warnings.append(f"{col_id}: contains {inf_count} infinite (+/-Inf) value(s)")
            else:
                coerced = pd.to_numeric(series, errors="coerce")
                inf_count = int(np.isinf(coerced.to_numpy(dtype=float, na_value=np.nan)).sum())
                if inf_count > 0:
                    quality_warnings.append(f"{col_id}: contains {inf_count} infinite (+/-Inf) value(s)")

                # Check formula-like string cells
                formula_cells = int(
                    series.dropna()
                    .astype(str)
                    .map(lambda s: bool(s) and s[0] in _FORMULA_PREFIXES)
                    .sum()
                )
                if formula_cells > 0:
                    quality_warnings.append(
                        f"{col_id}: contains {formula_cells} formula-prefixed cell(s); sanitize on export"
                    )

        safe_df = df.copy()
        safe_df.columns = [f"col_{idx:02d}" for idx in range(1, col_count + 1)]

        profile = DatasetProfile(
            dataset_id=assigned_id,
            dataset_hash=dataset_hash,
            row_count=row_count,
            col_count=col_count,
            columns=columns,
            quality_warnings=quality_warnings,
            candidate_group_cols=candidate_group_cols,
        )

        self._profiles[assigned_id] = profile
        self._dataframes[assigned_id] = safe_df
        self._col_maps[assigned_id] = col_map
        return profile

    def get_profile(self, dataset_id: str) -> DatasetProfile:
        if dataset_id not in self._profiles:
            raise SecurityError(f"Unknown dataset_id: '{dataset_id}'")
        return self._profiles[dataset_id]

    def get_dataframe(self, dataset_id: str) -> pd.DataFrame:
        """返回只读副本，列名已完全替换为安全 col_id。"""
        if dataset_id not in self._dataframes:
            raise SecurityError(f"Unknown dataset_id: '{dataset_id}'")
        return self._dataframes[dataset_id].copy(deep=True)

    def get_column_mapping(self, dataset_id: str, col_id: str) -> ColumnMapping:
        if dataset_id not in self._col_maps:
            raise SecurityError(f"Unknown dataset_id: '{dataset_id}'")
        col_map = self._col_maps[dataset_id]
        if col_id not in col_map:
            raise SecurityError(f"Unknown col_id '{col_id}' for dataset '{dataset_id}'")
        return col_map[col_id]

    def resolve_col_id_by_name(self, dataset_id: str, sanitized_name: str) -> Optional[str]:
        """辅助方法：按清洗后列名反查安全 col_id。"""
        profile = self.get_profile(dataset_id)
        for col in profile.columns:
            if col.sanitized_name == sanitized_name:
                return col.col_id
        return None
