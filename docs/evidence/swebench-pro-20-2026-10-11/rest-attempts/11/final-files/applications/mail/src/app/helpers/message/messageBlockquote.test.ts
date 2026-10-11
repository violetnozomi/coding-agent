import mails from './__fixtures__/messageBlockquote.fixtures';
import { locateBlockquote } from './messageBlockquote';

/**
 * Creating a whole document each time is needed because locate blockquote is using xpath request
 * which will fail if the content is not actually in the document
 */
const createDocument = (content: string) => {
    const newDocument = document.implementation.createHTMLDocument();
    newDocument.body.innerHTML = content;
    return newDocument.body;
};

describe('messageBlockquote', () => {
    Object.entries(mails as { [name: string]: string }).forEach(([name, content]) => {
        it(`should find the blockquote in the mail ${name}`, () => {
            const [, blockquote] = locateBlockquote(createDocument(content));
            expect(blockquote.length).not.toBe(0);
        });
    });

    it(`should correctly detect proton blockquote with default font and no signature`, () => {
        const content = `
            <div style="font-family: verdana; font-size: 20px;">
                <div style="font-family: verdana; font-size: 20px;"><br></div>
                <div class="protonmail_signature_block protonmail_signature_block-empty" style="font-family: verdana; font-size: 20px;">
                    <div class="protonmail_signature_block-user protonmail_signature_block-empty"></div>
                    <div class="protonmail_signature_block-proton protonmail_signature_block-empty"></div>
                </div>
                <div style="font-family: verdana; font-size: 20px;"><br></div>
                <div class="protonmail_quote">
                    ------- Original Message -------<br>
                    On Tuesday, January 4th, 2022 at 17:13, Swiip - Test account &lt;swiip.test@protonmail.com&gt; wrote:<br>
                    <blockquote class="protonmail_quote" type="cite">
                        <div style="font-family: verdana; font-size: 20px;">
                            <div style="font-family: verdana; font-size: 20px;">test</div>
                            <div class="protonmail_signature_block protonmail_signature_block-empty" style="font-family: verdana; font-size: 20px;">
                                <div class="protonmail_signature_block-user protonmail_signature_block-empty"></div>
                                <div class="protonmail_signature_block-proton protonmail_signature_block-empty"></div>
                            </div>
                        </div>
                    </blockquote><br>
                </div>
            </div>`;

        const [before, after] = locateBlockquote(createDocument(content));

        expect(before).not.toContain('Original Message');
        expect(after).toContain('Original Message');
    });

    describe('content after the blockquote', () => {
        const blockquote = '<blockquote class="protonmail_quote"><div>quoted content</div></blockquote>';

        it('should not select a blockquote followed by non-empty text', () => {
            const content = `<div>Hello</div>${blockquote}<div>text after the quote</div>`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toBe('');
            expect(before).toContain('Hello');
            expect(before).toContain('text after the quote');
        });

        it('should select a blockquote followed only by empty text', () => {
            const content = `<div>Hello</div>${blockquote}<div>   <br></div>`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toContain('quoted content');
            expect(before).toContain('Hello');
            expect(before).not.toContain('quoted content');
        });

        it('should not select a blockquote followed by an image anchor, even without text', () => {
            const content = `<div>Hello</div>${blockquote}<span class="proton-image-anchor" data-proton-remote="1"></span>`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toBe('');
            expect(before).toContain('quoted content');
            expect(before).toContain('proton-image-anchor');
        });

        it('should select the last blockquote when several are present', () => {
            const first = '<blockquote class="protonmail_quote"><div>quote one</div></blockquote>';
            const second = '<blockquote class="protonmail_quote"><div>quote two</div></blockquote>';
            const content = `<div>Hello</div>${first}<div>between the quotes</div>${second}`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toContain('quote two');
            expect(after).not.toContain('quote one');
            // The content preceding the selected quote, including the earlier
            // blockquote, is kept as the non-quoted content
            expect(before).toContain('quote one');
            expect(before).toContain('between the quotes');
        });

        it('should return the whole message when no blockquote qualifies', () => {
            const content = `<div>Hello</div>${blockquote}<div>text after the quote</div><span class="proton-image-anchor"></span>`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toBe('');
            expect(before).toEqual(content.trim());
        });

        it('should handle quotes with identical content separated by text', () => {
            const first = '<blockquote class="protonmail_quote"><div>same content</div></blockquote>';
            const second = '<blockquote class="protonmail_quote"><div>same content</div></blockquote>';
            const content = `<div>Hello</div>${first}<div>between</div>${second}`;

            const [before, after] = locateBlockquote(createDocument(content));

            expect(after).toContain('same content');
            expect(before).toContain('between');
            expect(before).toContain('Hello');
        });
    });
});
