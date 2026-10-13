// expect: line 4: environment cannot be empty
pipeline {
    agent any
    environment { }
    stages { stage('A') { steps { echo 'x' } } }
}
