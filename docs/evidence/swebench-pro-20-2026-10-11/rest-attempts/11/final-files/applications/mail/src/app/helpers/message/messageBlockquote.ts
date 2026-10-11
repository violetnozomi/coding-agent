export const BLOCKQUOTE_SELECTORS = [
    '.protonmail_quote', // Proton Mail
    // Gmail creates both div.gmail_quote and blockquote.gmail_quote. The div
    // version marks text but does not cause indentation, but both should be
    // considered quoted text.
    '.gmail_quote', // Gmail
    'div.gmail_extra', // Gmail
    'div.yahoo_quoted', // Yahoo Mail
    'blockquote.iosymail', // Yahoo iOS Mail
    '.tutanota_quote', // Tutanota Mail
    '.zmail_extra', // Zoho
    '.skiff_quote', // Skiff Mail
    '#divRplyFwdMsg', // Outlook Mail
    'div[id="3D\\"divRplyFwdMsg\\""]', // Office365
    'hr[id=replySplit]',
    '.moz-cite-prefix',
    'div[id=isForwardContent]',
    'blockquote[id=isReplyContent]',
    'div[id=mailcontent]',
    'div[id=origbody]',
    'div[id=reply139content]',
    'blockquote[id=oriMsgHtmlSeperator]',
    'blockquote[type="cite"]',
    '[name="quote"]', // gmx
];

const BLOCKQUOTE_TEXT_SELECTORS = ['-----Original Message-----'];

const BLOCKQUOTE_SELECTOR = BLOCKQUOTE_SELECTORS.map((selector) => `${selector}:not(:empty)`).join(',');

/**
 * Selector matching the anchors inserted in place of images. Those elements
 * carry no text of their own, so they must be detected explicitly when looking
 * for content that follows a blockquote.
 */
const IMAGE_ANCHOR_SELECTOR = '.proton-image-anchor';

/**
 * Returns content before and after match in the source
 * Beware, String.prototype.split does almost the same but will not if there is several match
 */
export const split = (source: string, match: string): [string, string] => {
    const index = source.indexOf(match);
    if (index === -1) {
        return [source, ''];
    }
    return [source.slice(0, index), source.slice(index + match.length)];
};

const searchForContent = (element: Element, text: string) => {
    const xpathResult = element.ownerDocument?.evaluate(
        `//*[text()='${text}']`,
        element,
        null,
        XPathResult.ORDERED_NODE_ITERATOR_TYPE,
        null
    );
    const result: Element[] = [];
    let match = null;
    // eslint-disable-next-line no-cond-assign
    while ((match = xpathResult?.iterateNext())) {
        result.push(match as Element);
    }
    return result;
};

/**
 * Return the HTML located before the given element inside the document.
 * Using a range (instead of a plain string split) makes the result stable even
 * when the element markup appears several times in the message.
 */
const getContentBefore = (document: Element, element: Element) => {
    const ownerDocument = document.ownerDocument;
    if (!ownerDocument) {
        return document.innerHTML || '';
    }

    const range = ownerDocument.createRange();
    range.setStart(document, 0);
    range.setEndBefore(element);

    const container = ownerDocument.createElement('div');
    container.appendChild(range.cloneContents());

    return container.innerHTML;
};

/**
 * Inspect the content of the document located after the given element. Both the
 * remaining text and the presence of an image anchor are relevant: an image
 * anchor contributes no text but still counts as content following the element.
 */
const getContentAfter = (document: Element, element: Element) => {
    const ownerDocument = document.ownerDocument;
    if (!ownerDocument) {
        return { text: '', hasImageAnchor: false };
    }

    const range = ownerDocument.createRange();
    range.setStartAfter(element);
    range.setEnd(document, document.childNodes.length);

    const fragment = range.cloneContents();

    return {
        text: fragment.textContent || '',
        hasImageAnchor: !!fragment.querySelector(IMAGE_ANCHOR_SELECTOR),
    };
};

/**
 * Try to locate the eventual blockquote present in the document no matter the expeditor of the mail
 * Return the HTML content splitted at the blockquote start
 */
export const locateBlockquote = (inputDocument: Element | undefined): [content: string, blockquote: string] => {
    if (!inputDocument) {
        return ['', ''];
    }

    const body = inputDocument.querySelector('body');
    const document = body || inputDocument;

    const parentHTML = document.innerHTML || '';
    // The last blockquote that is not followed by any other content is the one
    // to collapse. It must however not replace an already selected blockquote
    // that contains it (nested quotes are collapsed as a whole).
    let selected: Element | null = null;

    const isCandidate = (blockquote: Element) => {
        const { text, hasImageAnchor } = getContentAfter(document, blockquote);

        if (text.trim().length || hasImageAnchor) {
            return false;
        }

        return !selected || !selected.contains(blockquote);
    };

    const record = (blockquote: Element) => {
        if (isCandidate(blockquote)) {
            selected = blockquote;
        }
    };

    // Standard search with a composed query selector
    [...document.querySelectorAll(BLOCKQUOTE_SELECTOR)].forEach(record);

    // Second search based on text content with xpath
    if (selected === null) {
        BLOCKQUOTE_TEXT_SELECTORS.forEach((text) => {
            searchForContent(document, text).forEach(record);
        });
    }

    if (selected === null) {
        return [parentHTML, ''];
    }

    const blockquoteHTML = selected.outerHTML || '';

    return [getContentBefore(document, selected), blockquoteHTML];
};
