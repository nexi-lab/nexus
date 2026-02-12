# outline-pdf-data-structure

## Table of contents

<!--#region toc-->

- [Table of contents](#table-of-contents)
- [Installation](#installation)
- [Description](#description)
- [Code coverage](#code-coverage)
- [Example](#example)
- [Documentation](#documentation)
    - [Concretions](#concretions)
        - [outlinePdfDataStructure](#----outlinepdfdatastructure)
- [Motivation](#motivation)
- [Contributing](#contributing)
- [Changelog](#changelog)
    - [1.0.3](#103)
    - [1.0.2](#102)
    - [1.0.1](#101)
    - [1.0.0](#100)
- [License](#license)

<!--#endregion toc-->

## Installation

```bash
npm install @lillallol/outline-pdf-data-structure
```

## Description

Creates a pdf outline data structure as defined in the pdf specification, from a human readable string representation of the pdf outline. You can then use that data structure to "hydrate" a real pdf outline data structure like it is done in [@lillallol/outline-pdf](https://github.com/lillallol/outline-pdf).

## Code coverage

The testing code coverage is around 90%.

## Example

<!--#region example !./src/example/example.test.ts-->

```ts
import { outlinePdfDataStructure } from "../";

describe(outlinePdfDataStructure.name, () => {
    it("returns low level information about the provided outline string representation", () => {
        expect(
            outlinePdfDataStructure(
                `
                    1||Document
                    2|-|Section 1
                   	3|-|Section 2
                    4|--|Subsection 1
                    5|-|Section 3
                    6||Summary
                `,
                6
            )
        ).toEqual({
            outlineRootCount: 6,
            outlineItems: [
                {
                    Title: "Document",
                    Parent: -1,
                    Next: 5,
                    First: 1,
                    Last: 4,
                    Count: 4,
                    Dest: 1 - 1,
                },
                {
                    Title: "Section 1",
                    Parent: 0,
                    Next: 2,
                    Dest: 2 - 1,
                },
                {
                    Title: "Section 2",
                    Parent: 0,
                    Prev: 1,
                    Next: 4,
                    First: 3,
                    Last: 3,
                    Count: 1,
                    Dest: 3 - 1,
                },
                {
                    Title: "Subsection 1",
                    Parent: 2,
                    Dest: 4 - 1,
                },
                {
                    Title: "Section 3",
                    Parent: 0,
                    Prev: 2,
                    Dest: 5 - 1,
                },
                {
                    Title: "Summary",
                    Parent: -1,
                    Prev: 0,
                    Dest: 6 - 1,
                },
            ],
        });
    });
});

```

<!--#endregion example-->

## Documentation

<!--#region documentation ./documentation.md-->

<h3 id="-concretions">Concretions</h3>
<h4 id="-concretion-outlinePdfDataStructure">
    outlinePdfDataStructure
</h4>

```ts
/**
 * @description
 * It returns all the information needed to create a real pdf data structure.
 */
export declare function outlinePdfDataStructure(
    inputOutline: string,
    totalNumberOfPages: number
): outlinePdfDataStructureReturnType;

```

<details open="">
<summary id="-concretion-outlinePdfDataStructure-references">
    <a href="#-concretion-outlinePdfDataStructure-references">#</a>
    references
</summary>

<br>

<blockquote>
<details>
<summary id="-concretion-outlinePdfDataStructure-references-outlinePdfDataStructureReturnType">
    <a href="#-concretion-outlinePdfDataStructure-references-outlinePdfDataStructureReturnType">#</a>
    <b>outlinePdfDataStructureReturnType</b>
</summary>
        
```ts
export declare type outlinePdfDataStructureReturnType = {
    /**
     * @description
     * It returns a low level programmatic representation of the outline.
     */
    outlineItems: outlineItem[];
    /**
     * @description
     * The total number of outline nodes.
     */
    outlineRootCount: number;
};
```



</details>
<blockquote>
<details>
<summary id="-concretion-outlinePdfDataStructure-references-outlinePdfDataStructureReturnType-outlineItem">
    <a href="#-concretion-outlinePdfDataStructure-references-outlinePdfDataStructureReturnType-outlineItem">#</a>
    <b>outlineItem</b>
</summary>
        
```ts
export declare type outlineItem = {
    /**
     * @description
     * The title that will be visible in the outline of the pdf for the context
     * outline node.
     */
    Title: string;
    /**
     * @description
     * The index (of the array that contains all the outline nodes) of the
     * parent outline node of the context outline node.
     *
     * Outline nodes of depth `0` have `-1` for this value.
     */
    Parent: number;
    /**
     * @description
     * The index (of the array that contains all the outline nodes) of the
     * previous sibling of the context outline node. It is `undefined` for the
     * case there is no previous sibling.
     */
    Prev?: number;
    /**
     * @description
     * The index (of the array that contains all the outline nodes) of the next
     * sibling of the context outline node. It is `undefined` for the case there
     * is no next sibling.
     */
    Next?: number;
    /**
     * @description
     * The index (of the array that contains all the outline nodes) of the first
     * immediate child of the context outline node. It is `undefined` for the
     * case there is no immediate child.
     */
    First?: number;
    /**
     * @description
     * The index (of the array that contains all the outline nodes) of the last
     * immediate child of the context outline node. It is `undefined` for the
     * case there is no immediate child.
     */
    Last?: number;
    /**
     * @description
     * Total number of outline nodes that are descendants to the context outline
     * node.
     */
    Count?: number;
    /**
     * @description
     * The page of the pdf that the outline node hyper links to.
     */
    Dest: number;
};
```



</details>

</blockquote>
</blockquote>
</details>
<hr>


<!--#endregion documentation -->

## Motivation

This whole package was part of [@lillallol/outline-pdf](https://github.com/lillallol/outline-pdf), but then I decide to do separation of concerns, because it would make both projects more readable.

## Contributing

I am open to suggestions/pull request to improve this program.

You will find the following commands useful:

-   Clones the github repository of this project:

    ```bash
    git clone https://github.com/lillallol/outline-pdf-data-structure
    ```

-   Installs the node modules (nothing will work without them):

    ```bash
    npm install
    ```

-   Tests the code and generates code coverage:

    ```bash
    npm run test
    ```

    The generated code coverage is saved in `./coverage`.

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

## Changelog

### 1.0.3

-   Added documentation section in `README.md`. The documentation was generated via [ts-doc-gen-md](https://www.npmjs.com/package/ts-doc-gen-md) and added in the `README.md` via [md-in-place](https://www.npmjs.com/package/md-in-place).
-   Added changelog section in `README.md`.
-   Added contributing section in `README.md`.
-   Added example section in `README.md`.
-   Did some internal changes in the code (added `tagUnindent`, created files: `errorMessages.ts`,`constants.ts`,`publicApi.ts`).

### 1.0.2

**bug fixes**

-   Changed count to be number of descendants instead of immediate descendants.

### 1.0.1

Some minor link fixes in the `README.md`.

### 1.0.0

First release.

## License

MIT
