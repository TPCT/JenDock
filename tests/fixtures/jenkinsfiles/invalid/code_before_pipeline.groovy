// expect: line 2: outside pipeline
def helper() { return 1 }
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } }
}
