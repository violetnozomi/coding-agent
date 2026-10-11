/*
Copyright 2024 New Vector Ltd.

SPDX-License-Identifier: AGPL-3.0-only OR GPL-3.0-only
Please see LICENSE files in the repository root for full details.
*/

import { ReactNode } from "react";
import { createRoot, Root } from "react-dom/client";

/**
 * Manages a set of independent React roots which are used to render dynamic
 * subtrees into arbitrary DOM nodes that live outside of the main React tree.
 *
 * Each root is created with `createRoot` (React 18+) and both the root and its
 * container element are tracked, so that consumers can deduplicate against the
 * elements which have already been processed and unmount everything later.
 */
export class ReactRootManager {
    private roots: Root[] = [];
    private containerElements: Element[] = [];

    /**
     * Render the given children into the supplied DOM element using a new React
     * root, tracking both the root and the element for later traversal and
     * unmounting.
     *
     * @param children - the React nodes to render
     * @param element - the DOM element to mount the React root into
     */
    public render(children: ReactNode, element: Element): void {
        const root = createRoot(element);
        root.render(children);
        this.roots.push(root);
        this.containerElements.push(element);
    }

    /**
     * @returns the DOM elements currently used as containers for the mounted
     * React roots.
     */
    public get elements(): Element[] {
        return this.containerElements;
    }

    /**
     * Unmount all managed React roots and clear the tracked roots and container
     * elements.
     */
    public unmount(): void {
        for (const root of this.roots) {
            root.unmount();
        }
        this.roots = [];
        this.containerElements = [];
    }
}
