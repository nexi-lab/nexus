# outline-pdf

> **NOTICE**: If you have outlined your pdf using a version from 2.0.0 to 3.0.3, then if you edit your pdf it will get corrupted due to a bug introduced by my module. From version 3.0.4 this bug has been resolved.

## Table of contents

<!--#region toc-->

- [Table of contents](#table-of-contents)
- [Installation](#installation)
- [Description](#description)
- [Code coverage](#code-coverage)
- [Example](#example)
- [Documentation](#documentation)
    - [Concretions](#concretions)
        - [outlinePdfFactory](#----outlinepdffactory)
- [Motivation](#motivation)
- [You might find interesting](#you-might-find-interesting)
- [Acknowledgments](#acknowledgments)
- [FAQs](#faqs)
- [Contributing](#contributing)
    - [Help needed](#help-needed)
- [Changelog](#changelog)
    - [4.0.0](#400)
    - [3.0.4](#304)
    - [3.0.3](#303)
    - [3.0.2](#302)
    - [3.0.1](#301)
    - [3.0.0](#300)
    - [2.0.4](#204)
    - [2.0.3](#203)
    - [2.0.2](#202)
    - [2.0.1](#201)
    - [2.0.0](#200)
    - [1.0.2](#102)
    - [1.0.1](#101)
    - [1.0.0](#100)
- [License](#license)

<!--#endregion toc-->

## Installation

```bash
npm install @lillallol/outline-pdf
```

## Description

> Take a look at [outline-pdf-cjs](https://www.npmjs.com/package/@lillallol/outline-pdf-cjs) if you are using node.

Adds outline to pdf that do not have one. As of right now, it is the only module in the npm registry that has the following characteristics :

-   Only javascript dependencies. No code from other programming languages is used.
-   Enables collapsing and nesting in the outline.
-   Fast download from the npm registry.
-   High level API.
-   Works in both the browser and node.

## Code coverage

Testing code coverage is more than 90%.

## Example

<!--#region example !./src/index.test.ts-->

```ts
import * as fs from "fs";
import * as path from "path";
import * as pdfLib from "pdf-lib";

import { outlinePdfFactory } from ".";

jest.setTimeout(60 * 10e3);

describe(outlinePdfFactory.name, () => {
    it("adds the provided outline to the loaded pdf", async () => {
        const outlinePdf = outlinePdfFactory(pdfLib);
        const absolutePathToPdfToOutline = path.resolve(__dirname, "test.pdf");
        const absolutePathToSaveOutlinedPdf = path.resolve(__dirname, "test.outline.pdf");
        try {
            fs.unlinkSync(absolutePathToSaveOutlinedPdf);
        } catch (e) {
            //file does not exist so no problem
        }
        const pdf = fs.readFileSync(absolutePathToPdfToOutline, { encoding: "base64" });
        const outline: string = `
             1||Some random title 1
             2|-|Some random title 2
            -3|--|Some random title 3
             4|---|Some random title 4
             5|---|Some random title 5
             6|-|Some random title 6
             7||Some random title 7
        `;
        const outlinedPdf = await outlinePdf({ outline, pdf }).then((pdfDocument) => pdfDocument.save());
        fs.writeFileSync(absolutePathToSaveOutlinedPdf, outlinedPdf);
    });
});

```

<!--#endregion example-->

## Documentation

<!--#region documentation ./documentation.md -->

<h3 id="-concretions">Concretions</h3>
<h4 id="-concretion-outlinePdfFactory">
    outlinePdfFactory
</h4>

```ts
export declare const outlinePdfFactory: IOutlinePdfFactory;

```

<details open="">
<summary id="-concretion-outlinePdfFactory-references">
    <a href="#-concretion-outlinePdfFactory-references">#</a>
    references
</summary>

<br>

<blockquote>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory">#</a>
    <b>IOutlinePdfFactory</b>
</summary>
        
```ts
export declare type IOutlinePdfFactory = (pdfLib: pdfLib) => IOutlinePdf;
```




</details>
<blockquote>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib">#</a>
    <b>pdfLib</b>
</summary>
        
```ts
/**
 * @description
 * Just use:
 *
 * ```ts
 * import * as pdfLib from "pdf-lib";
 * ```
 */
export declare type pdfLib = {
    PDFDict: typeof PDFDict;
    PDFName: typeof PDFName;
    PDFNumber: typeof PDFNumber;
    PDFRef: typeof PDFRef;
    PDFArray: typeof PDFArray;
    PDFNull: typeof PDFNull;
    PDFDocument: typeof PDFDocument;
    PDFPageLeaf: typeof PDFPageLeaf;
    PDFHexString: typeof PDFHexString;
};
```











</details>
<blockquote>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFDict">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFDict">#</a>
    <b>PDFDict</b>
</summary>
        
```ts
import type { PDFDict } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFName">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFName">#</a>
    <b>PDFName</b>
</summary>
        
```ts
import type { PDFName } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFNumber">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFNumber">#</a>
    <b>PDFNumber</b>
</summary>
        
```ts
import type { PDFNumber } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFRef">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFRef">#</a>
    <b>PDFRef</b>
</summary>
        
```ts
import type { PDFRef } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFArray">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFArray">#</a>
    <b>PDFArray</b>
</summary>
        
```ts
import type { PDFArray } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFNull">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFNull">#</a>
    <b>PDFNull</b>
</summary>
        
```ts
import type { PDFNull } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFDocument">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFDocument">#</a>
    <b>PDFDocument</b>
</summary>
        
```ts
import type { PDFDocument } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFPageLeaf">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFPageLeaf">#</a>
    <b>PDFPageLeaf</b>
</summary>
        
```ts
import type { PDFPageLeaf } from "pdf-lib";
```



</details>
<details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFHexString">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFHexString">#</a>
    <b>PDFHexString</b>
</summary>
        
```ts
import type { PDFHexString } from "pdf-lib";
```



</details>

</blockquote><details>
<summary id="-concretion-outlinePdfFactory-references-IOutlinePdfFactory-IOutlinePdf">
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-IOutlinePdf">#</a>
    <b>IOutlinePdf</b>
</summary>
        
```ts
export declare type IOutlinePdf = (_: {
    /**
     * @description
     * The pdf to outline.
     *
     * I you provide the pdf as a string, then make sure it is of base 64.
     */
    pdf: string | Uint8Array | ArrayBuffer | PDFDocument;
    /**
     * @description
     * String representation of the outline.
     *
     * Example:
     *
     * ```ts
     * `
     *    1||some title 1
     *   12|-|some title 2
     *  -30|--|some title 3
     *   34|---|some title 4
     *   35|---|some title 5
     *   60|--|some title 6
     *   67|-|some title 7
     *   80||some title 8
     * `
     * ```
     *
     * where the:
     *
     * * first column is the page number, and if it negative it means that this
     *   part of the outline is collapsed
     * * second column is the outline depth
     * * third column is the outline title
     */
    outline: string;
}) => Promise<PDFDocument>;
```

<p>
    <a href="#-concretion-outlinePdfFactory-references-IOutlinePdfFactory-pdfLib-PDFDocument">
        <b>PDFDocument</b>
    </a>
</p>

</details>
<blockquote>

</blockquote>
</blockquote>
</blockquote>
</details>
<hr>


<!--#endregion documentation -->

## Motivation

I had to use this module in one of my projects: [scrap-it](https://www.npmjs.com/package/scrap-it). I could not find one that satisfied my needs, so I decided to create my own.

## You might find interesting

-   [@lillallol/outline-pdf-cjs](https://www.npmjs.com/package/@lillallol/outline-pdf-cjs)
    -   the equivalent of the current module for node
    -   uses the current module
-   [@lillallol/outline-pdf-data-structure](https://www.npmjs.com/package/@lillallol/outline-pdf-data-structure)
    -   creates a pdf outline data structure from a high level string representation of the pdf outline
    -   used by the current module
-   [pdf-lib](https://www.npmjs.com/package/pdf-lib)
    -   low level pdf library for both browser and node
-   [hummus-toc](https://www.npmjs.com/package/@ocelot-consulting/hummus-toc)
    -   uses [hummus](https://www.npmjs.com/package/hummus) which is deprecated
    -   only javascript dependencies
    -   takes a lot of time to download
-   [pdfjs](https://www.npmjs.com/package/pdfjs)
    -   only javascript dependencies
    -   downloads fast

## Acknowledgments

The following comments : [1](https://github.com/Hopding/pdf-lib/issues/127#issuecomment-502450179) , [2](https://github.com/Hopding/pdf-lib/issues/127#issuecomment-641710694) , contributed tremendously in the creation of this module.

## FAQs

<details>
<summary>Why do I have to inject <code>pdf-lib</code> to use this module.</summary>

No <code>pdf-lib</code> code is hard coded in this module. When someone wants to use this module and also use <code>pdf-lib</code>, to create a new module, there will be no <code>pdf-lib</code> code repeated in the generated bundle.

</details>

## Contributing

I am open to suggestions/pull request to improve this program.

You will find the following commands useful:

-   Clones the github repository of this project:

    ```bash
    git clone https://github.com/lillallol/outline-pdf
    ```

-   Installs the node modules (nothing will work without them):

    ```bash
    npm install
    ```

-   Tests the source code:

    ```bash
    npm run test
    ```

-   Lints the source folder using typescript and eslint:

    ```bash
    npm run lint
    ```

-   Builds the typescript code from the `./src` folder to javascript code in `./dist`:

    ```bash
    npm run build-ts
    ```

-   Injects in place the generated toc and imported files to `README.md`:

    ```bash
    npm run build-md
    ```

-   Checks the project for spelling mistakes:

    ```bash
    npm run spell-check
    ```

    Take a look at the related configuration `./cspell.json`.

-   Checks `./src` for dead typescript files:

    ```bash
    npm run dead-files
    ```

    Take a look at the related configuration `./unimportedrc.json`.

-   Logs in terminal which `dependencies` and `devDependencies` have a new version published in npm:

    ```bash
    npm run check-updates
    ```

-   Updates the `dependencies` and `devDependencies` to their latest version:

    ```bash
    npm run update
    ```

-   Formats all `.ts` files of the `./src` folder:

    ```bash
    npm run format
    ```

### Help needed

I want to:

-   read the outline of a pdf
-   delete the outline of a pdf

using `pdf-lib`.

Finally I want to create the following tests:

-   test that my module correctly outlines a pdf by reading the outline of the pdf that got outlined
-   test that after outlining the pdf, any kind of pdf editing (pdf annotations for example), will not corrupt the outlined pdf

## Changelog

### 4.0.0

**Breaking changes**

-   The interface of `outlinePdf` has been simplified.

**Other**

-   Replaced the `README.md` browser example, with the test file of the entry point function.
-   Added sections Code coverage, Documentation, Contributing in `README.md`.

### 3.0.4

**Bug fixes**

-   Esm entry point was importing module `@lillallol/outline-pdf-data-structure` without `.js` extension.
-   Fixed bug that was introduced from version 2.0.0 and was caused by creating more pdf refs than the number of outline nodes, which made the outlined pdf file get corrupted if it was edited with a pdf annotator.

**Other**

-   Added `CHANGELOG.md`.

### 3.0.3

**bug fixes**

-    Fixed bug that made the module crash if it had more outline nodes than pdf pages.

### 3.0.2

-   Updated dependencies.

### 3.0.1

**bug fixes**

-   Fixed the bug that made the outline work only if there was one outline node for each pdf page.

### 3.0.0

**breaking changes**

-   `outlinePdfCjs` is now on its own package: `@lillallol/outline-pdf-cjs`.

### 2.0.4

-   Internal changes.

### 2.0.3

**bug fixes**

-   Outline does not bug with non english characters now.

### 2.0.2

-   Minor changes in `README.md`.

### 2.0.1

-   Minor changes in `README.md`.

### 2.0.0

**Breaking changes**

-   `outlinePdf` is renamed to `outlinePdfCjsFactory`. You now have to inject `pdf-lib` in the factory.
-   added `outlinePdfFactory`. It can outline pdf in both browser and nodejs. You need to inject `pdf-lib` in the factory.

**Other**

-   added entry for esm

### 1.0.2

-   Added error messages for some edge cases.

### 1.0.1

-   Minor changes in `README.md`.

### 1.0.0

-   Published the package.

## License

MIT
