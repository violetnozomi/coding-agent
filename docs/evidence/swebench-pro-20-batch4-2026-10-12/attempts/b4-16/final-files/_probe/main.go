package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func main() {
	root := os.Args[1]
	needle := strings.ToLower(os.Args[2])
	filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if strings.Contains(strings.ToLower(d.Name()), needle) {
			fmt.Println(p)
		}
		return nil
	})
}
