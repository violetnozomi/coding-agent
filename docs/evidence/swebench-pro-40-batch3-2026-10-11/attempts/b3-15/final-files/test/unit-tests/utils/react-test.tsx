/*
Copyright 2024 New Vector Ltd.

SPDX-License-Identifier: AGPL-3.0-only OR GPL-3.0-only
Please see LICENSE files in the repository root for full details.
*/

import React from "react";
import { act } from "jest-matrix-react";

import { ReactRootManager } from "../../../src/utils/react";

describe("ReactRootManager", () => {
    it("starts with no tracked elements", () => {
        const manager = new ReactRootManager();
        expect(manager.elements).toHaveLength(0);
    });

    it("renders children into the element and tracks it", () => {
        const manager = new ReactRootManager();
        const container = document.createElement("div");

        act(() => {
            manager.render(<span>Hello world</span>, container);
        });

        expect(container.textContent).toBe("Hello world");
        expect(manager.elements).toEqual([container]);
    });

    it("tracks each element it renders into", () => {
        const manager = new ReactRootManager();
        const containerA = document.createElement("div");
        const containerB = document.createElement("div");

        act(() => {
            manager.render(<span>A</span>, containerA);
            manager.render(<span>B</span>, containerB);
        });

        expect(manager.elements).toEqual([containerA, containerB]);
    });

    it("unmounts all roots and clears its tracking", () => {
        const manager = new ReactRootManager();
        const container = document.createElement("div");

        act(() => {
            manager.render(<span>Hello world</span>, container);
        });
        expect(container.textContent).toBe("Hello world");

        act(() => {
            manager.unmount();
        });

        expect(container.textContent).toBe("");
        expect(manager.elements).toHaveLength(0);
    });
});
