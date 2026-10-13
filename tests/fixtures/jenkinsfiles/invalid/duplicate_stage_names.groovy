// expect: line 6: duplicate stage name 'Build'
pipeline {
    agent any
    stages {
        stage('Build') { steps { echo 'a' } }
        stage('Build') { steps { echo 'b' } }
    }
}
