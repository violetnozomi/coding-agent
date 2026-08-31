"""Tests for conservative multi-language repository structure extraction."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        (
            "service.ts",
            "export interface User { id: string }\n"
            "export class UserService {}\n"
            "export async function loadUser(id: string) {}\n"
            "export const saveUser = async (user: User) => user\n"
            "const workspaceSymbol = Effect.fn('workspaceSymbol')\n"
            "// function ignored() {}\n",
            {
                ("interface", "User"),
                ("class", "UserService"),
                ("function", "loadUser"),
                ("function", "saveUser"),
                ("constant", "workspaceSymbol"),
            },
        ),
        (
            "service.go",
            "type Store struct {}\n"
            "type Reader interface {}\n"
            "func NewStore() *Store { return nil }\n"
            "func (s *Store) Save() {}\n",
            {
                ("struct", "Store"),
                ("interface", "Reader"),
                ("function", "NewStore"),
                ("method", "Save"),
            },
        ),
        (
            "lib.rs",
            "pub struct Store {}\n"
            "pub enum State { Ready }\n"
            "pub trait Reader {}\n"
            "pub async fn load() {}\n",
            {
                ("struct", "Store"),
                ("enum", "State"),
                ("trait", "Reader"),
                ("function", "load"),
            },
        ),
        (
            "Service.java",
            "public interface Reader {}\n"
            "public final class Service {}\n"
            "enum State { READY }\n"
            "record User(String id) {}\n",
            {
                ("interface", "Reader"),
                ("class", "Service"),
                ("enum", "State"),
                ("record", "User"),
            },
        ),
        (
            "Service.kt",
            "data class User(val id: String)\n"
            "interface Reader\n"
            "object Registry\n"
            "suspend fun loadUser() {}\n",
            {
                ("class", "User"),
                ("interface", "Reader"),
                ("object", "Registry"),
                ("function", "loadUser"),
            },
        ),
        (
            "service.cpp",
            "struct Store {};\n"
            "class Service {};\n"
            "int load_value(int id) {\n"
            "if (ready) {\n",
            {
                ("struct", "Store"),
                ("class", "Service"),
                ("function", "load_value"),
            },
        ),
        (
            "service.rb",
            "module Api\n"
            "class Service\n"
            "def self.load!\n",
            {
                ("module", "Api"),
                ("class", "Service"),
                ("function", "load!"),
            },
        ),
        (
            "service.php",
            "<?php\n"
            "final class Service {}\n"
            "interface Reader {}\n"
            "public function loadUser() {}\n",
            {
                ("class", "Service"),
                ("interface", "Reader"),
                ("function", "loadUser"),
            },
        ),
        (
            "service.lua",
            "local function load_user(id)\n"
            "function Service:save()\n"
            "-- function ignored()\n",
            {
                ("function", "load_user"),
                ("function", "Service:save"),
            },
        ),
        (
            "service.sh",
            "function build() {\n"
            "deploy() {\n"
            "# ignored() {\n",
            {
                ("function", "build"),
                ("function", "deploy"),
            },
        ),
    ],
)
def test_extract_language_symbols(filename, source, expected):
    from nz_coder.tools.repo_languages import extract_language_symbols

    symbols = extract_language_symbols(Path(filename), source)

    assert {(symbol.kind, symbol.name) for symbol in symbols} == expected


def test_repo_map_indexes_mixed_language_directory(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_map import repo_map

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "service.ts").write_text(
        "export class UserService {}\n"
        "export const createUser = (id: string) => ({ id })\n",
        encoding="utf-8",
    )
    (tmp_path / "store.go").write_text(
        "type Store struct {}\n"
        "func NewStore() *Store { return nil }\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.rs").write_text(
        "pub trait Reader {}\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("class NotSource\n", encoding="utf-8")

    result = repo_map()

    assert result.startswith("Source repository map")
    assert "languages: go, rust, typescript" in result
    assert "service.ts:" in result
    assert "class UserService" in result
    assert "function createUser" in result
    assert "store.go:" in result
    assert "struct Store" in result
    assert "function NewStore" in result
    assert "lib.rs:" in result
    assert "trait Reader" in result
    assert "notes.txt" not in result


def test_repo_map_supports_non_python_file_and_query(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.tools.repo_map import repo_map

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    source = tmp_path / "service.ts"
    source.write_text(
        "export class Service {}\n"
        "export class ServiceFactory {}\n",
        encoding="utf-8",
    )

    result = repo_map("service.ts", query="Service")

    assert result.startswith("Source repository map")
    assert "languages: typescript" in result
    assert result.index("class Service:") < result.index("class ServiceFactory:")


def test_repo_map_semantic_probe_uses_ranked_non_python_file(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.lsp.workspace_symbols import WorkspaceSymbolResult
    from nz_coder.tools import repo_map as module

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    (tmp_path / "alpha.go").write_text(
        "type Alpha struct {}\n",
        encoding="utf-8",
    )
    target = tmp_path / "service.ts"
    target.write_text(
        "export class TargetService {}\n",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def collect(**kwargs):
        calls.append(kwargs)
        return WorkspaceSymbolResult(source="lsp/fake", symbols=())

    monkeypatch.setattr(module, "collect_workspace_symbols", collect)

    result = module.repo_map(query="TargetService", semantic=True)

    assert "class TargetService" in result
    assert calls[0]["probe"] == target
